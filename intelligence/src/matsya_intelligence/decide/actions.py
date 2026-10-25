"""Candidate actions: each one is a scenario transform list with an exact cost.

| action           | transforms                                       | cost (objective)                         |
|------------------|--------------------------------------------------|------------------------------------------|
| treds_discount   | invoice_cash replace: A - discount on day L      | the discount                             |
| chase            | rate_multiplier buyer x factor for N weeks       | chase cost                               |
| msmed_interest   | invoice_cash if_unpaid: P + interest on day lag  | penalty - recovery x P(unpaid) x interest |
| overdraft        | cash_event + k units on draw day d               | interest from d to day H + 1             |
| supplier_stretch | date_shift supplier_payments, share s, N days    | penalty + MSMED interest + 43B(h) delay  |

Overdraft draws are integer (k units of limit / units); everything else is binary. Overdraft
headroom enters only here, as draws with their interest; the shortfall threshold is the owner
cushion and never includes it. A claim's objective cost can be negative (the interest is worth
more than the penalty); the plans table holds max(cost, 0) because cost_paise is non-negative
there, and the decision result keeps the signed figure as net_cost_paise.

Effects on balances (deltas against the baseline, path by path) for treds_discount,
msmed_interest and overdraft are exact from the baseline's per-invoice payment days. Chase and
stretch deltas come from scenario runs on the baseline's seed; the engine's per-row uniforms keep
a chased buyer's change from moving any other buyer's payments.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl

from matsya_intelligence.decide import costs as C
from matsya_intelligence.engine.core import EngineInputs, EngineResult
from matsya_intelligence.scenarios.transforms import CashEvent, DateShift, InvoiceCash, RateMultiplier

MsmeClass = Literal["micro", "small", "medium", "none"]
EntityType = Literal["proprietor", "partnership", "company"]


@dataclass(frozen=True)
class Supplier:
    """One supplier, as the owner describes it. share = its fraction of supplier payments."""

    supplier_id: str
    share: float
    msme_class: MsmeClass = "none"
    terms_days: int = 30
    penalty_paise: int | None = None  # owner-set; None = constants default
    tightened: bool = False  # owner says this supplier tightened terms: never stretch it
    terms_in_writing: bool = True  # s.15: written terms set the deadline; otherwise 15 days

    @property
    def msmed_deadline_days(self) -> int:
        """s.15 deadline in days from acceptance: the written terms capped at 45, else 15.
        # VERIFY: s.15 wording (agreed date in writing, at most 45 days; else the appointed day)."""
        return min(self.terms_days, 45) if self.terms_in_writing else 15


@dataclass
class DecisionInputs:
    """What the decision engine needs beyond the simulation engine's inputs.

    invoices: open invoices, index-aligned with EngineInputs.open_invoice_ids
    (invoice_id, buyer_id, issue_date, due_date, msmed_deadline_days).
    """

    tenant_msme_class: MsmeClass
    invoices: pl.DataFrame
    buyer_types: dict[str, str]
    treds_onboarded: set[str] = field(default_factory=set)
    overdraft_limit_paise: int = 0
    overdraft_rate: float | None = None
    suppliers: list[Supplier] = field(default_factory=list)
    eps: float | None = None
    day: int | None = None
    bank_rates: list | None = None  # (effective_from, Fraction); None = constants file
    entity_type: EntityType | None = None  # sets the default income-tax rate (company only)
    tax_rate: float | None = None  # owner's income-tax rate; overrides the entity default
    cost_of_funds: float | None = None  # annual; None = the overdraft rate


@dataclass
class Action:
    kind: str  # ActionKind value
    target_id: str
    cost_paise: int  # objective cost per unit (claims: may be negative)
    ub: int = 1
    unit_paise: int = 0  # overdraft only
    invoice: int | None = None  # open invoice index (treds_discount, msmed_interest)
    buyer: int | None = None  # buyer index (chase)
    supplier: str | None = None
    detail: dict = field(default_factory=dict)
    _transforms: list = field(default_factory=list)

    def transforms(self, k: int = 1) -> list:
        if self.kind == "overdraft":
            t = self._transforms[0]
            return [t.model_copy(update={"amount_paise": k * self.unit_paise})] if k else []
        return list(self._transforms) if k else []

    def cost(self, k: int = 1) -> int:
        """Exact cost of taking the action k times (overdraft: k units drawn together)."""
        if self.kind == "overdraft":
            return C.overdraft_interest(k * self.unit_paise, self.detail["rate"], self.detail["days_used"])
        return self.cost_paise * k


def _plain(x):
    return x.item() if hasattr(x, "item") else x


def candidate_actions(inp: EngineInputs, dinp: DecisionInputs, base: EngineResult, horizon: int,
                      active_transforms: list) -> list[Action]:
    """Every action open to the owner at as_of. base: the subsample baseline (flows kept)."""
    k = C.constants()
    as_of = inp.as_of
    hist = dinp.bank_rates if dinp.bank_rates is not None else C.bank_rate_history()
    oi = inp.open_invoices
    meta = dinp.invoices
    if meta["invoice_id"].to_list() != list(inp.open_invoice_ids):
        raise ValueError("decision invoices must be index-aligned with the engine's open invoices")
    acts: list[Action] = []

    # TReDS discount: buyer on TReDS, invoice not yet due after the acceptance lag.
    rate, lag = k["treds"]["discount_rate_annual"]["value"], k["treds"]["acceptance_lag_days"]["value"]
    on_treds = set(k["treds"]["buyer_types"]["value"])
    for i, row in enumerate(meta.iter_rows(named=True)):
        amount = int(oi.cash[i])
        if amount <= 0:
            continue
        b = row["buyer_id"]
        if not (dinp.buyer_types.get(b) in on_treds or b in dinp.treds_onboarded):
            continue
        days_early = (row["due_date"] - as_of).days - lag
        if days_early <= 0:
            continue
        cash, cost = C.treds_discount(amount, rate, days_early)
        acts.append(Action("treds_discount", row["invoice_id"], cost, invoice=i,
                           detail={"amount_paise": amount, "cash_paise": cash, "days_early": days_early,
                                   "rate": rate, "on_day": lag},
                           _transforms=[InvoiceCash(invoice_id=row["invoice_id"], on=as_of + timedelta(days=lag),
                                                    amount_paise=cash, mode="replace")]))

    # Chase: every buyer with open cash.
    ch = k["chase"]
    for b_i, b in enumerate(inp.buyer_ids):
        if oi.cash[oi.buyer == b_i].sum() <= 0:
            continue
        acts.append(Action("chase", b, ch["cost_paise"]["value"], buyer=b_i,
                           detail={"factor": ch["factor"]["value"], "weeks": ch["weeks"]["value"],
                                   "open_paise": int(oi.cash[oi.buyer == b_i].sum())},
                           _transforms=[RateMultiplier(target="buyer", ids=[b], factor=ch["factor"]["value"],
                                                       weeks=ch["weeks"]["value"])]))

    tax = C.income_tax_rate(dinp.entity_type, dinp.tax_rate)

    # MSMED claim: micro/small tenant, invoice past its s.15 deadline. The recovered interest is
    # taxable income, so its value is net of tax at the owner's rate.
    if dinp.tenant_msme_class in C.MSMED_SUPPLIERS:
        mc = k["msmed_claim"]
        claim_day = mc["cash_lag_days"]["value"]
        pay = base.open_pay
        for i, row in enumerate(owed_interest(inp, dinp, hist).iter_rows(named=True)):
            if row["interest_to_as_of_paise"] is None:
                continue
            if tax is None:
                raise ValueError("MSMED claims are open and their interest is taxable: give the income-tax rate "
                                 "(tax_rate) or entity_type='company'")
            principal = row["principal_paise"]
            appointed = row["appointed_day"]
            interest = C.msmed_interest(principal, appointed, as_of + timedelta(days=claim_day), hist)
            idx = inp.open_invoice_ids.index(row["invoice_id"])
            unpaid = float(((pay[:, idx] < 0) | (pay[:, idx] > claim_day)).mean())
            penalty = C.to_paise(principal * C.frac(mc["relationship_penalty_share"]["value"]))
            # VERIFY: interest recovered under s.16 is taxable income of the supplier.
            value = C.to_paise(C.frac(unpaid) * C.frac(mc["recovery_share"]["value"]) * interest * (1 - tax))
            acts.append(Action("msmed_interest", row["invoice_id"], penalty - value, invoice=idx,
                               detail={"principal_paise": principal, "interest_paise": interest, "value_paise": value,
                                       "penalty_paise": penalty, "p_unpaid_by_claim_day": unpaid,
                                       "on_day": claim_day},
                               _transforms=[InvoiceCash(invoice_id=row["invoice_id"],
                                                        on=as_of + timedelta(days=claim_day),
                                                        amount_paise=principal + interest, mode="if_unpaid")]))

    # Overdraft: integer units per draw day, repaid after the horizon.
    od = k["overdraft"]
    od_rate = dinp.overdraft_rate if dinp.overdraft_rate is not None else od["rate_annual"]["value"]
    units = od["units"]["value"]
    unit = dinp.overdraft_limit_paise // units
    if unit > 0:
        for d in od["draw_days"]["value"]:
            if d > horizon:
                continue
            days_used = horizon + 1 - d
            acts.append(Action("overdraft", f"day{d}", C.overdraft_interest(unit, od_rate, days_used), ub=units,
                               unit_paise=unit, detail={"draw_day": d, "days_used": days_used, "rate": od_rate},
                               _transforms=[CashEvent(on=as_of + timedelta(days=d), amount_paise=unit,
                                                      label="overdraft draw")]))

    # Supplier stretch: never for a supplier the owner or the active scenario says tightened terms.
    if not any(t.type == "terms_change" and t.party == "supplier" for t in active_transforms):
        st = k["stretch"]
        purch = -base.flows["purchases"][:, 1:].astype(float) if "purchases" in base.flows else None
        cof = dinp.cost_of_funds if dinp.cost_of_funds is not None else od_rate
        for s in dinp.suppliers:
            if s.tightened or purch is None:
                continue
            if tax is None and s.msme_class in C.MSMED_SUPPLIERS:
                raise ValueError(f"supplier {s.supplier_id} is micro/small: a stretch past its s.15 deadline can defer "
                                 "the 43B(h) deduction, so give the income-tax rate (tax_rate) or entity_type='company'")
            for n in st["days"]["value"]:
                cost, det = stretch_cost(s, n, purch, as_of, hist, tax, cof, inp.gst_rate)
                acts.append(Action("supplier_stretch", f"{s.supplier_id}:+{n}d", cost, supplier=s.supplier_id,
                                   detail={**det, "days": n, "share": s.share},
                                   _transforms=[DateShift(target="supplier_payments", days=n, share=s.share)]))
    return acts


def stretch_cost(s: Supplier, n: int, purch: np.ndarray, as_of: date, hist, tax_rate, cost_of_funds,
                 gst_rate: float) -> tuple[int, dict]:
    """Owner penalty, plus for a micro/small supplier paid past its s.15 deadline (written terms
    capped at 45 days, else 15; Supplier.msmed_deadline_days):

    - MSMED s.16 interest on the expected stretched amount for the late days the stretch adds, at
      full cost: s.23 makes it non-deductible, so no tax saving is netted off it;
    - the delay in the s.43B(h) / 37(2)(g) deduction. Paid late, the purchase is deductible in
      the FY of payment instead of the FY of acceptance. That is a deferral, not a loss: a late
      payment in the same FY costs nothing, one moved across 31 Mar costs
      tax_rate x amount net of GST x cost_of_funds x (FYs the deduction moves).

    purch: (paths, H) supplier payments by day (positive, GST included), the baseline's. Payment
    day t is accepted t - terms days earlier (issue date stands in for acceptance).
    """
    k = C.constants()["stretch"]
    penalty = s.penalty_paise if s.penalty_paise is not None else k["penalty_paise"]["value"]
    by_day = s.share * purch.mean(axis=0)  # expected stretched amount per day 1..H
    amount = C.to_paise(C.frac(float(by_day.sum())))
    deadline = s.msmed_deadline_days
    late_before = max(s.terms_days - deadline, 0)
    late_after = max(s.terms_days + n - deadline, 0)
    interest = deferral = 0
    amount_years = 0.0
    if s.msme_class in C.MSMED_SUPPLIERS and late_after > 0:
        start = as_of + timedelta(days=1)
        interest = (C.msmed_interest(amount, start, start + timedelta(days=late_after), hist)
                    - C.msmed_interest(amount, start, start + timedelta(days=late_before), hist))
        # VERIFY: s.43B(h) (37(2)(g) from 1 Apr 2026) scope and its reading of the s.15 limit.
        for t in range(1, len(by_day) + 1):
            pay_old = as_of + timedelta(days=t)
            accepted = pay_old - timedelta(days=s.terms_days)
            fy_before = C.fiscal_year(pay_old) if late_before else C.fiscal_year(accepted)
            fy_after = C.fiscal_year(pay_old + timedelta(days=n))
            amount_years += by_day[t - 1] * max(fy_after - fy_before, 0)
        if amount_years > 0:
            if tax_rate is None or cost_of_funds is None:
                raise ValueError("the 43B(h) deferral needs an income-tax rate and a cost of funds")
            deferral = C.deduction_deferral_cost(C.frac(amount_years) / (1 + C.frac(gst_rate)), tax_rate,
                                                 cost_of_funds)
    return penalty + interest + deferral, {"amount_paise": amount, "penalty_paise": penalty,
                                           "msmed_interest_paise": interest, "deferral_paise": deferral,
                                           "deferred_amount_years_paise": C.to_paise(C.frac(amount_years)),
                                           "deadline_days": deadline, "late_days_before": late_before,
                                           "late_days": late_after}


def owed_interest(inp: EngineInputs, dinp: DecisionInputs, hist=None) -> pl.DataFrame:
    """MSMED interest the buyers owe on overdue open invoices at as_of, whether or not a claim is chosen.

    Null interest: not past the deadline, or the tenant is not micro/small.
    """
    hist = hist if hist is not None else (dinp.bank_rates if dinp.bank_rates is not None else C.bank_rate_history())
    rows = []
    eligible = dinp.tenant_msme_class in C.MSMED_SUPPLIERS
    for i, r in enumerate(dinp.invoices.iter_rows(named=True)):
        principal = int(inp.open_invoices.cash[i])
        # VERIFY: the s.15 clock starts at acceptance; issue date stands in for it.
        appointed = r["issue_date"] + timedelta(days=int(r["msmed_deadline_days"]) + 1)
        overdue = eligible and appointed <= inp.as_of and principal > 0
        rows.append({"tenant_id": inp.tenant_id, "invoice_id": r["invoice_id"], "buyer_id": r["buyer_id"],
                     "principal_paise": principal, "appointed_day": appointed,
                     "days_overdue": max((inp.as_of - appointed).days + 1, 0),
                     "interest_to_as_of_paise": C.msmed_interest(principal, appointed, inp.as_of + timedelta(days=1), hist)
                     if overdue else None})
    return pl.DataFrame(rows, schema={"tenant_id": pl.String, "invoice_id": pl.String, "buyer_id": pl.String,
                                      "principal_paise": pl.Int64, "appointed_day": pl.Date, "days_overdue": pl.Int64,
                                      "interest_to_as_of_paise": pl.Int64})


def invoice_delta(base: EngineResult, inp: EngineInputs, a: Action, horizon: int) -> np.ndarray:
    """(paths, H) balance change from an invoice action, exact from the baseline's payment days."""
    pay = base.open_pay[:, a.invoice]
    cash = int(inp.open_invoices.cash[a.invoice])
    t = a.transforms()[0]
    day, amt = (t.on - inp.as_of).days, t.amount_paise
    days = np.arange(1, horizon + 1)[None, :]
    changed = np.ones(len(pay), dtype=bool) if t.mode == "replace" else (pay < 0) | (pay > day)
    old = np.where((pay[:, None] >= 1) & (days >= pay[:, None]), cash, 0)
    new = np.where(days >= day, amt, 0) if day <= horizon else np.zeros_like(days)
    return np.where(changed[:, None], new - old, 0).astype(np.int64)


def chase_invoice_delta(base: EngineResult, chased: EngineResult, inp: EngineInputs, i: int,
                        horizon: int) -> np.ndarray:
    """(paths, H) balance change of open invoice i between the baseline and a chase run."""
    cash = int(inp.open_invoices.cash[i])
    days = np.arange(1, horizon + 1)[None, :]

    def cum(pay):
        return np.where((pay[:, None] >= 1) & (days >= pay[:, None]), cash, 0)

    return (cum(chased.open_pay[:, i]) - cum(base.open_pay[:, i])).astype(np.int64)


def decision_inputs(tables: dict[str, pl.DataFrame], inp: EngineInputs, tenant_msme_class: MsmeClass,
                    **kw) -> DecisionInputs:
    """DecisionInputs from contract tables, as-of reads through the adapters. kw: the owner's inputs
    (overdraft_limit_paise, overdraft_rate, suppliers, eps, day, treds_onboarded)."""
    from matsya_intelligence.io.adapters import track_b_inputs

    obs = track_b_inputs(tables, inp.as_of, inp.gst_rate)
    inv = obs["invoices"].select("invoice_id", "buyer_id", "issue_date", "due_date", "msmed_deadline_days")
    order = pl.DataFrame({"invoice_id": list(inp.open_invoice_ids)}).with_row_index("_i")
    inv = order.join(inv, on="invoice_id", how="left").sort("_i").drop("_i")
    onboarded = set(kw.pop("treds_onboarded", set()))
    tb = tables.get("ref.tenant_buyers")
    if tb is not None and tb.height:
        onboarded |= set(tb.filter((pl.col("tenant_id") == inp.tenant_id) & pl.col("treds_onboarded"))["buyer_id"])
    return DecisionInputs(tenant_msme_class=tenant_msme_class, invoices=inv,
                          buyer_types=dict(zip(obs["buyers"]["buyer_id"], obs["buyers"]["type"].cast(pl.String))),
                          treds_onboarded=onboarded, **kw)
