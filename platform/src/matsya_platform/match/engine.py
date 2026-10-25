"""A6 matcher: a cascade over credits and open invoices (docs/matcher-design.md).

Stage 1 (reference) runs first over every credit carrying a resolvable invoice reference.
The remaining receivable credits then go through, in date order: single-invoice fits
(exact gross, TDS-explained, TReDS discount), bounded bundles, and part-payments, with the
decision made by margin over the runner-up. Accepted matches update each invoice's book
(allocated, inferred TDS, discount fee), so later credits see what is still open.

The whole cascade runs up to em_passes times: each pass learns every buyer's habitual
(rate, basis) from the previous pass's accepted fits and penalises other hypotheses,
until the accepted set stops changing. Credits and invoices are sorted internally, so the
result does not depend on input order.
"""

import itertools
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime

import polars as pl

from matsya_contracts import tables as T
from matsya_contracts.enums import MatchMethod, PayerKind, SettlementKind
from matsya_contracts.validate import frame
from matsya_platform.match import references
from matsya_platform.match.buyers import PATH_CONFIDENCE, CreditBuyerResolver
from matsya_platform.match.config import MatchConfig

Hyp = tuple[float, str]  # (rate, basis)
# ops.events has no kind for this yet (positions doc F12); returned in MatchResult.events.
SETTLED_INVOICE_CONTRADICTION = "settled_invoice_contradiction"
ARITHMETIC_STAGES = ("exact", "tds", "treds", "bundle")
# Why the TDS basis cannot tell an advance from a payment, added to a review reason.
BLIND_SPOT = {
    "none_zero_tds": "no TDS basis signal: at 0% TDS both bases give the same rupees, so the basis cannot show "
                     "whether this is an advance",
    "none_within_tolerance": "no TDS basis signal: TDS rate x GST is within the basis tolerance, so the two bases "
                             "cannot be told apart",
}
STAGE_CONFIDENCE = {"reference": 0.98, "exact": 0.95, "tds": 0.9, "treds": 0.8, "bundle": 0.7, "partial": 0.6}
# MatchMethod has no member per cascade stage (positions doc F9).
METHOD_OF_STAGE = {"reference": MatchMethod.REFERENCE, "exact": MatchMethod.AMOUNT_WINDOW,
                   "tds": MatchMethod.AMOUNT_WINDOW, "treds": MatchMethod.AMOUNT_WINDOW,
                   "partial": MatchMethod.AMOUNT_WINDOW, "bundle": MatchMethod.IP_ASSIGNMENT}


@dataclass(frozen=True)
class Credit:
    txn_uid: str
    value_date: date
    known_at: datetime
    amount: int
    narration: str
    buyer_id: str | None
    path: str | None
    path_conf: float
    platform: bool
    refs: tuple[str, ...]


@dataclass(frozen=True)
class Inv:
    invoice_id: str
    buyer_id: str
    issue_date: date
    due_date: date
    gross: int
    gst: int
    treds_eligible: bool

    def tds(self, h: Hyp) -> int:
        rate, basis = h
        return round(rate * (self.gross - self.gst if basis == "taxable_value" else self.gross))


@dataclass(frozen=True)
class Alloc:
    invoice_id: str
    alloc: int
    tds: int
    fee: int


@dataclass(frozen=True)
class Explanation:
    stage: str  # reference | exact | tds | treds | bundle | partial
    invoice_ids: tuple[str, ...]
    hyp: Hyp | None
    cost: int
    unexplained_paise: int
    allocs: tuple[Alloc, ...]
    payer_kind: str
    # What the TDS basis says about this fit: "taxable" (invoice-payment shaped), "gross_only"
    # (advance shaped), "none_zero_tds" (0% gives the same rupees on either base) or
    # "none_within_tolerance" (rate x GST is within tolerance, so the bases cannot be told apart).
    basis_signal: str | None = None

    @property
    def gross_only(self) -> bool:
        return self.basis_signal == "gross_only"


@dataclass(frozen=True)
class ReviewItem:
    txn_uid: str
    value_date: date
    amount_paise: int
    buyer_id: str | None
    reason: str
    candidates: tuple[Explanation, ...]  # ranked, best first
    referenced: bool = False  # the credit carried a resolvable invoice reference


@dataclass
class _Entry:
    credit: Credit
    expl: Explanation
    alloc: Alloc
    confidence: float


@dataclass
class _Book:
    entries: list[_Entry] = field(default_factory=list)
    hyp: Hyp | None = None

    @property
    def allocated(self) -> int:
        return sum(e.alloc.alloc for e in self.entries)

    @property
    def tds(self) -> int:
        return sum(e.alloc.tds for e in self.entries)

    @property
    def fee(self) -> int:
        return sum(e.alloc.fee for e in self.entries)

    def settled(self) -> int:
        return self.allocated + self.tds + self.fee


@dataclass(frozen=True)
class MatchResult:
    matches: pl.DataFrame  # ledger.matches rows
    outcomes: pl.DataFrame  # labels.invoice_outcomes rows, one version per invoice at this run
    review: list[ReviewItem]
    excluded_invoices: set[str]  # invoices in a review item: no training label is written
    links: pl.DataFrame  # one row per accepted allocation, with the cascade stage
    stats: dict
    events: list[dict] = field(default_factory=list)  # settled_invoice_contradiction (F12)
    # invoice_id -> referenced | arithmetic_only | arithmetic_no_basis_signal | mixed, for every
    # labelled invoice with a link (weakest: arithmetic_no_basis_signal).
    # labels.invoice_outcomes has no field for it (positions doc F14).
    label_evidence: dict = field(default_factory=dict)
    review_ranked: list[dict] = field(default_factory=list)  # review.rank(): risk-ordered items with evidence
    review_groups: list[dict] = field(default_factory=list)
    review_coverage: dict = field(default_factory=dict)  # share of at-risk rupees in the top N items


def _credits(transactions: pl.DataFrame, as_of: datetime, resolver: CreditBuyerResolver, invoices: dict[str, Inv],
             tenant_id: str, supplier_gstin: str, cfg: MatchConfig,
             categories: dict[str, tuple[str, float]]) -> tuple[list[Credit], int]:
    """(credits the matcher sees, credits the categoriser gate removed). A credit the
    categoriser labels confidently as something other than a receipt never reaches the
    matcher; no label, abstain or low confidence falls back to the receivable test."""
    known = set(invoices)
    out, gated = [], 0
    rows = transactions.filter((pl.col("amount_paise") > 0) & (pl.col("known_at") <= as_of)).sort(
        "value_date", "known_at", "txn_uid")
    for r in rows.iter_rows(named=True):
        cat = categories.get(r["txn_uid"]) if cfg.category_gate else None
        if cat and cat[0] not in (*cfg.receipt_categories, "abstain") and cat[1] >= cfg.category_min_confidence:
            gated += 1
            continue
        res = resolver.resolve(r["narration_raw"])
        refs = tuple(references.resolve(r["narration_raw"], r["value_date"], tenant_id, supplier_gstin,
                                        cfg.window_days, known))
        out.append(Credit(r["txn_uid"], r["value_date"], r["known_at"], r["amount_paise"], r["narration_raw"] or "",
                          res.buyer_id, res.path, res.confidence, res.platform, refs))
    return out, gated


def _receivable(c: Credit) -> bool:
    """A credit is treated as a possible receipt only with a buyer, a platform rail or a reference."""
    return c.buyer_id is not None or c.platform or bool(c.refs)


class _Matcher:
    def __init__(self, credits: list[Credit], invoices: dict[str, Inv], cfg: MatchConfig,
                 habits: dict[str, Hyp]):
        self.cfg, self.habits = cfg, habits
        self.credits, self.invoices = credits, invoices
        self.books: dict[str, _Book] = {i: _Book() for i in invoices}
        self.by_buyer: dict[str, list[Inv]] = {}
        for inv in sorted(invoices.values(), key=lambda v: (v.issue_date, v.invoice_id)):
            self.by_buyer.setdefault(inv.buyer_id, []).append(inv)
        self.treds_invoices = [i for b in sorted(self.by_buyer) for i in self.by_buyer[b] if i.treds_eligible]
        self.review: list[ReviewItem] = []
        self.events: list[dict] = []

    # ---------------------------------------------------------------- helpers

    def _open(self, inv: Inv) -> bool:
        return self.books[inv.invoice_id].settled() < inv.gross - self.cfg.tolerance_paise

    def _in_window(self, inv: Inv, c: Credit) -> bool:
        return inv.issue_date <= c.value_date <= inv.issue_date.fromordinal(inv.issue_date.toordinal()
                                                                           + self.cfg.window_days)

    def _hyps(self, inv: Inv) -> list[tuple[Hyp, int]]:
        """(hypothesis, penalty). A book already part-paid keeps its hypothesis open; the
        buyer's learned habit carries no penalty, anything else the habit penalty."""
        habit = self.habits.get(inv.buyer_id)
        out = []
        for rate in self.cfg.tds_rates:
            for basis in self.cfg.tds_bases:
                pen = 0 if habit is None or habit == (rate, basis) else self.cfg.habit_penalty_paise
                out.append(((rate, basis), pen))
        seen, uniq = {}, []
        for h, pen in out:  # same rupees under two bases (no GST): keep the cheaper, habit first
            key = inv.tds(h)
            if key not in seen or (pen, h[1] != "taxable_value") < seen[key][1]:
                seen[key] = (h, (pen, h[1] != "taxable_value"))
        for key in sorted(seen):
            h, (pen, _) = seen[key]
            uniq.append((h, pen))
        return uniq

    def _remaining_net(self, inv: Inv, h: Hyp) -> int:
        b = self.books[inv.invoice_id]
        return inv.gross - inv.tds(h) - b.allocated - b.fee

    def _payer(self, c: Credit, inv: Inv, short: bool) -> str:
        """Platform rail: short of net before the due date is a financier discount; full net
        at any date is the buyer's own payment routed through the platform (a financier pays
        early only because it discounted, and a discount always arrives short); short after
        the due date is unknown."""
        if not c.platform:
            return PayerKind.BUYER if c.buyer_id is not None or c.refs else PayerKind.UNKNOWN
        if not short:
            return PayerKind.BUYER
        if c.value_date < inv.due_date:
            return PayerKind.TREDS_FINANCIER
        return PayerKind.UNKNOWN

    def _singles(self, c: Credit, invs: list[Inv], stage_ref: bool) -> list[Explanation]:
        cfg, out = self.cfg, []
        for inv in invs:
            book = self.books[inv.invoice_id]
            best, full_fits = None, []
            for h, pen in self._hyps(inv):
                if book.hyp is not None and h != book.hyp:
                    continue
                rem = self._remaining_net(inv, h)
                tds_left = inv.tds(h) - book.tds
                diff = abs(c.amount - rem)
                if diff <= cfg.tolerance_paise:
                    stage = "reference" if stage_ref else ("exact" if h[0] == 0 and not book.entries else "tds")
                    e = Explanation(stage, (inv.invoice_id,), h, diff + pen, diff,
                                    (Alloc(inv.invoice_id, c.amount, tds_left, 0),), self._payer(c, inv, False))
                    full_fits.append(h)
                elif c.platform and c.amount < rem and rem - c.amount <= cfg.max_fee_share * rem \
                        and c.value_date < inv.due_date:
                    e = Explanation("reference" if stage_ref else "treds", (inv.invoice_id,), h,
                                    cfg.treds_penalty_paise + pen, 0,
                                    (Alloc(inv.invoice_id, c.amount, tds_left, rem - c.amount),),
                                    PayerKind.TREDS_FINANCIER)
                else:
                    continue
                if best is None or (e.cost, e.hyp) < (best.cost, best.hyp):
                    best = e
            if best is not None:
                out.append(replace(best, basis_signal=self._basis_signal(c, inv, best, full_fits)) if full_fits else best)
        return out

    def _basis_signal(self, c: Credit, inv: Inv, best: Explanation, full_fits: list[Hyp]) -> str:
        """Which base the credit fits, judged with basis_tolerance_paise rather than the amount
        tolerance: both bases can fit the amount within Rs 2 while one fits it exactly."""
        rate = best.hyp[0]
        if rate == 0:
            return "none_zero_tds"
        tol = self.cfg.basis_tolerance_paise
        if abs(inv.tds((rate, "gross")) - inv.tds((rate, "taxable_value"))) <= tol:
            return "none_within_tolerance"
        miss = {b: abs(c.amount - self._remaining_net(inv, (rate, b))) for b in ("taxable_value", "gross")}
        if miss["gross"] + tol < miss["taxable_value"]:
            return "gross_only"
        if miss["taxable_value"] + tol < miss["gross"]:
            return "taxable"
        return "none_within_tolerance"

    def _basis_suspect(self, e: Explanation) -> str | None:
        """The basis rule (see MatchConfig.basis_evidence). Fires only when the buyer's
        learned habit is taxable value: an unknown habit gives no signal, so the buyer's own
        accepted fits establish its practice first."""
        if not (self.cfg.basis_evidence and e.gross_only and e.stage in ("exact", "tds")):
            return None
        habit = self.habits.get(self.invoices[e.invoice_ids[0]].buyer_id)
        if habit is None or habit[1] != "taxable_value":
            return None
        return (f"basis evidence: fits only with TDS {e.hyp[0]:g} on the gross amount, how an advance is taxed; "
                f"this buyer deducts on taxable value")

    def _partials(self, c: Credit, invs: list[Inv], stage_ref: bool) -> list[Explanation]:
        out = []
        for inv in invs:
            book = self.books[inv.invoice_id]
            h = book.hyp or self.habits.get(inv.buyer_id)
            rem = self._remaining_net(inv, h) if h else inv.gross - book.allocated - book.fee
            if c.amount < rem - self.cfg.tolerance_paise:
                out.append(Explanation("reference" if stage_ref else "partial", (inv.invoice_id,), h,
                                       self.cfg.partial_penalty_paise, rem - c.amount,
                                       (Alloc(inv.invoice_id, c.amount, 0, 0),), self._payer(c, inv, True)))
        return out

    def _bundles(self, c: Credit, invs: list[Inv]) -> list[Explanation]:
        cfg = self.cfg
        pool = [i for i in invs if not self.books[i.invoice_id].entries]
        pool = sorted(pool, key=lambda i: (abs((c.value_date - i.issue_date).days), i.invoice_id))[:cfg.bundle_max_open]
        pool = sorted(pool, key=lambda i: (i.issue_date, i.invoice_id))
        out = []
        for k in range(2, cfg.bundle_cap + 1):
            for subset in itertools.combinations(pool, k):
                for rate in cfg.tds_rates:
                    for basis in cfg.tds_bases:
                        h = (rate, basis)
                        nets = [i.gross - i.tds(h) for i in subset]
                        diff = abs(c.amount - sum(nets))
                        if diff > cfg.tolerance_paise:
                            continue
                        habit = self.habits.get(subset[0].buyer_id)
                        pen = 0 if habit is None or habit == h else cfg.habit_penalty_paise
                        allocs = [Alloc(i.invoice_id, n, i.tds(h), 0) for i, n in zip(subset, nets)]
                        last = allocs[-1]
                        allocs[-1] = Alloc(last.invoice_id, last.alloc + c.amount - sum(nets), last.tds, 0)
                        out.append(Explanation("bundle", tuple(i.invoice_id for i in subset), h,
                                               diff + pen + cfg.bundle_penalty_paise * (k - 1), diff, tuple(allocs),
                                               self._payer(c, subset[0], False)))
        return out

    def _candidates(self, c: Credit) -> list[Inv]:
        if c.buyer_id is not None:
            invs = self.by_buyer.get(c.buyer_id, [])
        elif c.platform:
            invs = self.treds_invoices
        else:
            invs = []
        return [i for i in invs if self._in_window(i, c) and self._open(i)]

    @staticmethod
    def _rank(es: list[Explanation]) -> list[Explanation]:
        return sorted(es, key=lambda e: (e.cost, e.invoice_ids, e.hyp or (0.0, "")))

    def _accept(self, c: Credit, e: Explanation, margin: float | None):
        cfg = self.cfg
        mf = 1.0 if margin is None else min(1.0, margin / cfg.full_margin_paise)
        conf = round(max(0.05, (c.path_conf or PATH_CONFIDENCE["reference"]) * STAGE_CONFIDENCE[e.stage] * mf), 4)
        for a in e.allocs:
            book = self.books[a.invoice_id]
            book.entries.append(_Entry(c, e, a, conf))
            if e.hyp is not None and book.hyp is None and e.stage != "partial":
                book.hyp = e.hyp

    def _to_review(self, c: Credit, reason: str, ranked: list[Explanation]):
        top = ranked[0] if ranked else None
        if top is not None and top.stage in ARITHMETIC_STAGES and top.basis_signal in BLIND_SPOT:
            reason = f"{reason}; {BLIND_SPOT[top.basis_signal]}"
        self.review.append(ReviewItem(c.txn_uid, c.value_date, c.amount, c.buyer_id, reason, tuple(ranked[:5]),
                                      bool(c.refs)))

    def _suspicion(self, c: Credit, e: Explanation) -> list[str]:
        """Advance-shaped signs on an arithmetic-only fit: early against the due date (not for
        a TReDS discount, which is early by nature), a round figure, or a TDS hypothesis off
        the buyer's learned habit."""
        cfg, flags = self.cfg, []
        invs = [self.invoices[i] for i in e.invoice_ids]
        if e.stage != "treds" and any((i.due_date - c.value_date).days > cfg.advance_days_before_due for i in invs):
            flags.append(f"arrives over {cfg.advance_days_before_due} days before the due date")
        if any(c.amount % s == 0 for s in cfg.round_steps_paise):
            flags.append("round-figure amount")
        habit = self.habits.get(invs[0].buyer_id)
        if habit is not None and e.hyp is not None and e.hyp != habit:
            flags.append(f"TDS {e.hyp[0]:g} on {e.hyp[1]} is off the buyer's habit {habit[0]:g} on {habit[1]}")
        return flags

    def _settle_or_review(self, c: Credit, e: Explanation, margin: float | None, ranked: list[Explanation]):
        """Strictness decides whether an arithmetic-only fit may settle an invoice."""
        if e.stage in ARITHMETIC_STAGES and self.cfg.strictness == "strict":
            self._to_review(c, "strict: an arithmetic-only fit may not settle an invoice", ranked)
            return
        if e.stage in ARITHMETIC_STAGES and self.cfg.strictness == "balanced":
            flags = self._suspicion(c, e)
            if flags:
                self._to_review(c, "advance-shaped: " + "; ".join(flags), ranked)
                return
        self._accept(c, e, margin)

    def _contradictions(self, c: Credit) -> list[tuple[Inv, _Entry]]:
        """Invoices this unreferenced credit fits in full that a referenced credit dated after
        it has settled: in date order this credit would have taken them first."""
        out = []
        pool = self.by_buyer.get(c.buyer_id, []) if c.buyer_id is not None else (self.treds_invoices if c.platform else [])
        for inv in pool:
            if not self._in_window(inv, c) or self._open(inv):
                continue
            later = [e for e in self.books[inv.invoice_id].entries
                     if e.credit.refs and e.credit.value_date > c.value_date]
            if not later:
                continue
            if any(abs(c.amount - (inv.gross - inv.tds(h))) <= self.cfg.tolerance_paise for h, _ in self._hyps(inv)):
                out.append((inv, later[0]))
        return out

    # ---------------------------------------------------------------- the cascade

    def run(self):
        cfg = self.cfg
        rest = []
        for c in self.credits:  # stage 1: references
            invs = [self.invoices[i] for i in c.refs]
            invs = [i for i in invs if (c.buyer_id is None or i.buyer_id == c.buyer_id) and self._open(i)]
            fits = self._rank(self._singles(c, invs, True))
            if fits and fits[0].cost <= cfg.accept_cost:
                self._accept(c, fits[0], None)
                continue
            parts = self._partials(c, invs, True)
            if len(parts) == 1:
                self._accept(c, parts[0], None)
                continue
            rest.append(c)
        for c in rest:  # stages 2 to 8
            if not _receivable(c):
                continue
            invs = self._candidates(c)
            singles = self._rank(self._singles(c, invs, False))
            contra = self._contradictions(c) if cfg.contradiction_check and not c.refs else []
            if contra:
                # A referenced payment won the invoice this credit fits; the referenced
                # one wins explicitly, and this credit goes to review with the reason.
                for inv, winner in contra:
                    self.events.append({"kind": SETTLED_INVOICE_CONTRADICTION, "displaced_txn_uid": c.txn_uid,
                                        "invoice_id": inv.invoice_id, "referenced_txn_uid": winner.credit.txn_uid,
                                        "displaced_value_date": c.value_date,
                                        "referenced_value_date": winner.credit.value_date})
                names = ", ".join(inv.invoice_id[:8] for inv, _ in contra)
                self._to_review(c, f"settled-invoice contradiction: fits invoice {names}, which a later referenced "
                                   "payment settled", singles)
                continue
            if singles and singles[0].cost <= cfg.accept_cost:
                runner = singles[1] if len(singles) > 1 else None
                margin = None if runner is None else runner.cost - singles[0].cost
                if margin is None or margin > cfg.min_margin_paise:
                    suspect = self._basis_suspect(singles[0])
                    if suspect:
                        self._to_review(c, suspect, singles)
                    else:
                        self._settle_or_review(c, singles[0], margin, singles)
                else:
                    self._to_review(c, f"ambiguous: {sum(1 for s in singles if s.cost - singles[0].cost <= cfg.min_margin_paise)} "
                                       "invoices explain the credit equally well", singles)
                continue
            # Bundles: accepted only when exactly one subset fits exactly, under one TDS
            # hypothesis (hypotheses giving the same allocations count as one). Zero or
            # several fits have no unique arithmetic answer and go to review.
            fits = self._bundles(c, invs)
            if fits:
                by_subset: dict[tuple, dict[tuple, Explanation]] = {}
                for e in fits:
                    by_subset.setdefault(e.invoice_ids, {}).setdefault(e.allocs, e)
                ranked = self._rank([min(v.values(), key=lambda e: (e.cost, e.hyp)) for v in by_subset.values()])
                subsets = list(by_subset.values())
                if len(subsets) == 1 and len(subsets[0]) == 1:
                    self._settle_or_review(c, next(iter(subsets[0].values())), None, ranked)
                elif len(subsets) == 1:
                    self._to_review(c, f"ambiguous bundle: one subset fits under {len(subsets[0])} TDS hypotheses",
                                    ranked)
                else:
                    self._to_review(c, f"ambiguous bundle: {len(by_subset)} invoice subsets fit exactly", ranked)
                continue
            parts = self._partials(c, invs, False)
            if parts:
                # A part-payment without a reference has no arithmetic that identifies its
                # invoice (it always leaves a remainder), and a unique candidate is not
                # evidence. It goes to review; completions still match above as exact fits.
                self._to_review(c, f"part-payment without reference: {len(parts)} open invoice(s) could absorb it",
                                self._rank(parts))
            else:
                self._to_review(c, "no explanation: no open invoice, TDS hypothesis or subset fits",
                                self._rank(singles))
        return self

    def accepted(self) -> list[_Entry]:
        return sorted((e for b in self.books.values() for e in b.entries),
                      key=lambda e: (e.credit.value_date, e.credit.txn_uid, e.alloc.invoice_id))

    def learn_habits(self) -> dict[str, Hyp]:
        """Each buyer's most common hypothesis among accepted fits that carry TDS evidence."""
        counts: dict[str, Counter] = {}
        for e in self.accepted():
            if e.expl.hyp is not None and e.expl.stage != "partial":
                counts.setdefault(self.invoices[e.alloc.invoice_id].buyer_id, Counter())[e.expl.hyp] += 1
        return {b: max(sorted(cnt), key=lambda h: cnt[h]) for b, cnt in sorted(counts.items())}


def match(transactions: pl.DataFrame, invoices: pl.DataFrame, buyers: pl.DataFrame, tenant_buyers: pl.DataFrame | None,
          *, tenant_id: str, supplier_gstin: str, as_of: datetime, complete_through: date,
          config: MatchConfig = MatchConfig(), txn_category: pl.DataFrame | None = None) -> MatchResult:
    """txn_category: features.txn_category rows, when available, for the categoriser gate."""
    cfg = config
    btype = dict(zip(buyers["buyer_id"], buyers["type"]))
    mandated = dict(zip(buyers["buyer_id"], buyers["treds_mandated"]))
    inv_rows = invoices.filter(pl.col("known_at") <= as_of).sort("known_at").group_by(
        "invoice_id", maintain_order=True).last().sort("issue_date", "invoice_id")
    invs = {r["invoice_id"]: Inv(r["invoice_id"], r["buyer_id"], r["issue_date"], r["due_date"], r["gross_paise"],
                                 r["gst_paise"] or 0, btype.get(r["buyer_id"]) == "cpse" or bool(mandated.get(r["buyer_id"])))
            for r in inv_rows.iter_rows(named=True)}
    resolver = CreditBuyerResolver(buyers, tenant_buyers, tenant_id, cfg.treds_platforms)
    categories = {} if txn_category is None else {
        t: (c, conf) for t, c, conf in txn_category.select("txn_uid", "category", "confidence").iter_rows()}
    credits, gated = _credits(transactions, as_of, resolver, invs, tenant_id, supplier_gstin, cfg, categories)

    habits: dict[str, Hyp] = {}
    m, previous, passes = None, None, 0
    for passes in range(1, cfg.em_passes + 1):
        m = _Matcher(credits, invs, cfg, habits).run()
        key = [(e.credit.txn_uid, e.alloc) for e in m.accepted()]
        if key == previous:
            break
        previous, habits = key, m.learn_habits()
    out = _outputs(m, invs, credits, tenant_id, as_of, complete_through, passes, cfg)
    out.stats["credits_gated_by_category"] = gated
    return out


def _outputs(m: _Matcher, invs: dict[str, Inv], credits: list[Credit], tenant_id: str, as_of: datetime,
             complete_through: date, passes: int, cfg: MatchConfig) -> MatchResult:
    entries = m.accepted()
    matches, links = [], []
    for e in entries:
        rate, basis = e.expl.hyp if e.expl.hyp and e.expl.stage != "partial" else (None, None)
        matches.append(T.Match(tenant_id=tenant_id, txn_uid=e.credit.txn_uid, invoice_id=e.alloc.invoice_id,
                               allocated_paise=e.alloc.alloc, tds_inferred_paise=e.alloc.tds,
                               confidence=e.confidence, method=METHOD_OF_STAGE[e.expl.stage],
                               payer_kind=e.expl.payer_kind, tds_rate=rate, tds_basis=basis, known_at=as_of))
        links.append({"txn_uid": e.credit.txn_uid, "invoice_id": e.alloc.invoice_id, "stage": e.expl.stage,
                      "allocated_paise": e.alloc.alloc, "tds_inferred_paise": e.alloc.tds, "fee_paise": e.alloc.fee,
                      "tds_rate": rate, "tds_basis": basis, "payer_kind": str(e.expl.payer_kind),
                      "buyer_path": e.credit.path, "confidence": e.confidence, "value_date": e.credit.value_date})
    excluded = set()
    for item in m.review:
        for cand in item.candidates[:2]:
            excluded.update(i for i in cand.invoice_ids if m._open(invs[i]))

    outcomes = []
    for inv in sorted(invs.values(), key=lambda v: (v.issue_date, v.invoice_id)):
        if inv.invoice_id in excluded:
            continue
        book = m.books[inv.invoice_id]
        es = sorted(book.entries, key=lambda e: (e.credit.value_date, e.credit.txn_uid))
        first = es[0].credit.value_date if es else None
        settled, paid = 0, None
        for e in es:
            settled += e.alloc.alloc + e.alloc.tds + e.alloc.fee
            if paid is None and settled >= inv.gross - cfg.tolerance_paise:
                paid = e.credit.value_date
        kinds = {str(e.expl.payer_kind) for e in es}
        treds = PayerKind.TREDS_FINANCIER in kinds
        if paid is None:
            kind, censored = None, max(complete_through, inv.issue_date)
        else:
            kind = (SettlementKind.TREDS_DISCOUNT if treds else
                    SettlementKind.BUYER if kinds == {PayerKind.BUYER} else SettlementKind.UNKNOWN)
            censored = min(e.credit.value_date for e in es if e.expl.payer_kind == PayerKind.TREDS_FINANCIER) \
                if treds else None
        outcomes.append(T.InvoiceOutcome(
            tenant_id=tenant_id, invoice_id=inv.invoice_id, buyer_id=inv.buyer_id, issue_date=inv.issue_date,
            first_paid_date=first, paid_date=paid, paid_fraction=min(settled / inv.gross, 1.0) if inv.gross else 1.0,
            settlement_kind=kind, censored_at=censored, known_at=as_of))
    considered = [c for c in credits if _receivable(c) or c.refs]
    allocated = Counter()
    for e in entries:
        allocated[e.credit.txn_uid] += e.alloc.alloc
    evidence = {}
    for inv_id, book in m.books.items():
        if inv_id in excluded or not book.entries:
            continue
        # Weakest first: an arithmetic-only link whose TDS basis says nothing (0% TDS, or the
        # bases within the basis tolerance) cannot tell an advance from a payment at all.
        kinds = {"referenced" if e.expl.stage == "reference" else
                 "arithmetic_no_basis_signal" if e.expl.basis_signal in BLIND_SPOT else "arithmetic_only"
                 for e in book.entries}
        if kinds == {"referenced"}:
            evidence[inv_id] = "referenced"
        elif "referenced" in kinds:
            evidence[inv_id] = "mixed"
        else:
            evidence[inv_id] = "arithmetic_no_basis_signal" if "arithmetic_no_basis_signal" in kinds else "arithmetic_only"
    from matsya_platform.match import review as rv
    ranked, groups, coverage = rv.rank(m, invs, credits, complete_through, cfg)
    stats = {"credits": len(credits), "credits_considered": len(considered), "credits_matched": len(allocated),
             "review_items": len(m.review), "passes": passes, "habits": len(m.habits),
             "unexplained_credit_paise": sum(c.amount - allocated.get(c.txn_uid, 0) for c in considered),
             "considered_credit_paise": sum(c.amount for c in considered)}
    return MatchResult(
        matches=frame(T.Match, matches), outcomes=frame(T.InvoiceOutcome, outcomes), review=m.review,
        excluded_invoices=excluded,
        links=pl.DataFrame(links, schema={"txn_uid": pl.String, "invoice_id": pl.String, "stage": pl.String,
                                          "allocated_paise": pl.Int64, "tds_inferred_paise": pl.Int64,
                                          "fee_paise": pl.Int64, "tds_rate": pl.Float64, "tds_basis": pl.String,
                                          "payer_kind": pl.String, "buyer_path": pl.String,
                                          "confidence": pl.Float64, "value_date": pl.Date}),
        stats=stats, events=m.events, label_evidence=evidence, review_ranked=ranked, review_groups=groups,
        review_coverage=coverage)
