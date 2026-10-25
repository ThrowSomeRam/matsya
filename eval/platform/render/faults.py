"""Named faults. Each has an intensity (the share of rows, payloads, windows or invoices it
hits) and draws from its own seeded stream, so changing one fault's rate never moves the
draws of another. (Window-shaping faults change which rows a window holds, so a row-level
choice inside a window, such as which row a partial window drops, can still differ.) A
rate of 0 turns a fault off.

Bank payload faults:
- duplicate_payload (payloads): delivered again one hour later under a new payload_id.
- drop_txn_id (rows): txnId omitted in every payload carrying the row. A4 falls back to
  the UTR/RRN reference, then the running balance, then the ordinal.
- drop_all_ids (rows): txnId, reference and currentBalance omitted: the ordinal path.
- drop_balances (rows): currentBalance omitted.
- drop_summary (payloads): the payload Summary omitted, so no bank_reported row and no
  end-of-window anchor.
- identical_rows (rows): a second, real transaction identical to the row in date, amount
  and narration (so also in its UTR/RRN), posted later the same IST day, with its own
  bank txnId. Both belong in the ledger.
- split_day_windows (windows): the window ends mid-day and the next window starts only
  0 to 4 hours before that cut, as a provider honouring a timestamp cursor would. The same
  day's rows then arrive across two payloads and ordinals shift between them. With the
  normal 3-day overlap this cannot happen (the earlier payload always holds both rows).
- partial_window (eligible windows): one row is missing while a later row is still there.
  Detectable when an anchor follows it (a later row's currentBalance or the Summary):
    batch_fails: A4 raises ContinuityError and writes nothing; the next window reaches
      back to the last successful fetch and the ledger recovers;
    accept_gap: the operator accepts the gap from the fault log and the next window,
      which overlaps the dropped row, recovers it.
  Undetectable when nothing anchors after it: A4 cannot see the gap, A2 believes the
  fetch succeeded and does not reach back, and the row is lost. Correct pipelines lose it
  too; what the sweep checks is whether anything says so.

- coincidental_credit (invoices): a credit from the invoice's real buyer that is not an
  invoice payment (an advance, a deposit, a round-figure transfer) at exactly the invoice's
  expected net under the buyer's real TDS rate, inside the candidate window and before the
  real payment. coincidental_basis sets which base its TDS is on: "gross" (default) is how
  an advance is taxed, since it is paid before any invoice shows GST separately (VERIFY:
  CBDT guidance on s.194Q); "taxable_value" is the old, unrealistic behaviour that makes an
  advance arithmetically identical to a payment. Narration names the buyer and carries no invoice
  reference. Truth records it as a non-invoice credit. Arithmetic cannot tell it from a
  payment; the fault measures what that costs.

Invoice CSV faults:
- duplicate_row (invoices): the invoice again right after itself, different whitespace.
- missing_gstin (invoices): buyer GSTIN blank; resolved by unique name, else rejected.

Combinations (FaultConfig.at): "combined" is every fault at one level; "no_anchors" is
drop_all_ids + drop_balances + drop_summary.
"""

import hashlib
import random
from dataclasses import dataclass, fields
from datetime import timedelta

import polars as pl

from matsya_contracts import ids
from render.windows import Window, WindowConfig, fetch_time, ist_date, ist_end, ist_start, rows_in

PARTIAL_MODES = ("batch_fails", "accept_gap")
FAULTS = ("duplicate_payload", "drop_txn_id", "drop_all_ids", "drop_balances", "drop_summary", "identical_rows",
          "split_day_windows", "partial_window", "coincidental_credit", "duplicate_row", "missing_gstin")
COMBOS = {"combined": FAULTS, "no_anchors": ("drop_all_ids", "drop_balances", "drop_summary")}


@dataclass(frozen=True)
class FaultConfig:
    seed: int = 0
    duplicate_payload_rate: float = 0.02
    drop_txn_id_rate: float = 0.02
    drop_all_ids_rate: float = 0.0
    drop_balances_rate: float = 0.0
    drop_summary_rate: float = 0.0
    identical_rows_rate: float = 0.0
    split_day_windows_rate: float = 0.0
    partial_window_rate: float = 0.03
    coincidental_credit_rate: float = 0.0
    coincidental_basis: str = "gross"  # gross | taxable_value
    duplicate_row_rate: float = 0.01
    missing_gstin_rate: float = 0.01
    partial_mode: str = "batch_fails"

    def __post_init__(self):
        if self.partial_mode not in PARTIAL_MODES:
            raise ValueError(f"partial_mode must be one of {PARTIAL_MODES}")
        for f in fields(self):
            if f.name.endswith("_rate") and not 0.0 <= getattr(self, f.name) <= 1.0:
                raise ValueError(f"{f.name} must be in [0, 1]")

    def rate(self, fault: str) -> float:
        return getattr(self, f"{fault}_rate")

    @classmethod
    def at(cls, level: float, only: str | None = None, **kw) -> "FaultConfig":
        """One fault, a named combination (COMBOS), or every fault (only=None) at level."""
        on = FAULTS if only is None else COMBOS.get(only, (only,))
        unknown = set(on) - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown fault(s) {sorted(unknown)}")
        return cls(**{f"{f}_rate": (level if f in on else 0.0) for f in FAULTS}, **kw)


def rng(seed: int, name: str) -> random.Random:
    return random.Random(int(hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()[:16], 16))


def plan_rows(uids: list[str], fault: str, cfg: FaultConfig) -> dict[str, str]:
    """txn_uid -> fault_id for a per-row fault, rows in posting order."""
    r, rate, out = rng(cfg.seed, fault), cfg.rate(fault), {}
    if rate <= 0:
        return out
    for uid in uids:
        if r.random() < rate:
            out[uid] = f"{fault}-{len(out):05d}"
    return out


def plan_clones(rows: pl.DataFrame, cfg: FaultConfig) -> tuple[pl.DataFrame, dict[str, str]]:
    """identical_rows: clone rows (in posting order) into rows of their own. Returns the
    clone rows (same columns) and clone txn_uid -> fault_id."""
    r, rate = rng(cfg.seed, "identical_rows"), cfg.rate("identical_rows")
    if rate <= 0:
        return rows.clear(), {}
    clones, fids = [], {}
    for row in rows.iter_rows(named=True):
        if r.random() >= rate:
            continue
        room = int((ist_end(ist_date(row["known_at"])) - row["known_at"]).total_seconds())
        known = row["known_at"] + timedelta(seconds=r.randrange(1, room) if room > 1 else 0)
        bank_id = f"{row['bank_txn_id']}D"
        uid = ids.txn_uid(row["tenant_id"], row["account_id"],
                          ids.txn_key_bank_id(bank_id, row["value_date"], row["amount_paise"]))
        clones.append({**row, "txn_uid": uid, "bank_txn_id": bank_id, "known_at": known})
        fids[uid] = f"identical_rows-{len(fids):05d}"
    return pl.DataFrame(clones, schema=rows.schema), fids


@dataclass(frozen=True)
class PartialPlan:
    fault_id: str
    window_index: int
    txn_uid: str  # the dropped row
    amount_paise: int
    break_on: object  # where A4 should date the break: IST posting date of the next anchored row, or the Summary's
    mode: str
    detectable: bool  # an anchor follows the dropped row


def plan_windows(account_id: str, acc_rows: pl.DataFrame, origin, ends, wcfg: WindowConfig, cfg: FaultConfig,
                 anchored, outcome=None) -> tuple[list[Window], dict[int, PartialPlan], set[int]]:
    """Build the account's windows and plan the window faults in one pass, because a failed
    or split window changes where the next one starts. anchored(txn_uid) says whether the
    row's currentBalance is rendered. Returns (windows, partial plans, windows without a
    Summary).

    outcome(window, plan, has_summary) -> bool says whether A2 believes the fetch
    succeeded. Open loop (outcome=None) predicts it from the plan: only a detectable
    batch_fails partial fails. Closed loop passes a callback that runs A4 on the payload,
    so A2 reacts to what actually happened and re-fetches from coverage after any failure."""
    r_part, r_split, r_sum = (rng(cfg.seed, f) for f in ("partial_window", "split_day_windows", "drop_summary"))
    wins, partials, no_summary = [], {}, set()
    last_ok, cursor = None, None  # end_ts of the last fetch A2 believes succeeded; timestamp cursor after a split
    for i, end in enumerate(ends):
        split_draw, cut_minute, back_minutes = r_split.random(), r_split.randrange(8 * 60, 20 * 60), r_split.randrange(241)
        if r_sum.random() < cfg.rate("drop_summary"):
            no_summary.add(i)
        if last_ok is None:
            start_ts = ist_start(origin)
        elif cursor is not None:
            start_ts = cursor
        else:
            start_ts = max(ist_start(origin), ist_start(ist_date(last_ok) - timedelta(days=wcfg.overlap_days - 1)))
        split = i < len(ends) - 1 and split_draw < cfg.rate("split_day_windows")
        if split:
            end_ts = ist_start(end) + timedelta(minutes=cut_minute)
            w = Window(account_id, i, start_ts, end_ts, end_ts + timedelta(minutes=30), split=True)
        else:
            w = Window(account_id, i, start_ts, ist_end(end), fetch_time(end, wcfg))
        wins.append(w)
        plan = None
        if 0 < i < len(ends) - 1 and cfg.rate("partial_window") > 0 and r_part.random() < cfg.rate("partial_window"):
            rows = rows_in(w, acc_rows)
            uids, known, posted = rows["txn_uid"].to_list(), rows["known_at"].to_list(), rows["posted_ist"].to_list()
            cands = [j for j in range(len(uids) - 1)
                     if (last_ok is None or known[j] > last_ok)
                     and (cfg.partial_mode == "batch_fails"
                          or (not split and posted[j] >= end - timedelta(days=wcfg.overlap_days - 1)))]
            if cands:
                j = cands[r_part.randrange(len(cands))]
                nxt = next((k for k in range(j + 1, len(uids)) if anchored(uids[k])), None)
                has_summary = i not in no_summary
                plan = PartialPlan(fault_id=f"partial_window-{len(partials):04d}", window_index=i, txn_uid=uids[j],
                                   amount_paise=int(rows["amount_paise"][j]),
                                   break_on=posted[nxt] if nxt is not None else ist_date(w.end_ts),
                                   mode=cfg.partial_mode, detectable=nxt is not None or has_summary)
                partials[i] = plan
        if outcome is None:
            ok = not (plan is not None and plan.detectable and plan.mode == "batch_fails")
        else:
            ok = outcome(w, plan, i not in no_summary)
        if ok:
            last_ok = w.end_ts
            cursor = w.end_ts - timedelta(minutes=back_minutes) if split else None
    return wins, partials, no_summary


def plan_duplicates(windows: list[Window], exclude: set[int], cfg: FaultConfig) -> dict[int, str]:
    """window index -> fault_id for payloads delivered twice."""
    r, rate, out = rng(cfg.seed, "duplicate_payload"), cfg.rate("duplicate_payload"), {}
    if rate <= 0:
        return out
    for w in windows:
        if r.random() < rate and w.index not in exclude:
            out[w.index] = f"duplicate_payload-{len(out):04d}"
    return out


def plan_invoice_rows(invoice_ids: list[str], fault: str, cfg: FaultConfig) -> dict[str, str]:
    """invoice_id -> fault_id for an invoice fault."""
    r, rate, out = rng(cfg.seed, fault), cfg.rate(fault), {}
    if rate <= 0:
        return out
    for inv in invoice_ids:
        if r.random() < rate:
            out[inv] = f"{fault}-{len(out):04d}"
    return out


def plan_coincidental(invoices: pl.DataFrame, buyers: pl.DataFrame, truth_payments: pl.DataFrame | None,
                      truth_buyers: pl.DataFrame | None, cut, template: dict, cfg: FaultConfig,
                      window_days: int = 120, truth_txns: pl.DataFrame | None = None
                      ) -> tuple[list[dict], dict[str, str], pl.DataFrame]:
    """coincidental_credit rows (World.rows() columns), txn_uid -> fault_id, and the truth table.

    Truth, per injected credit: the invoice it mimics, and about that invoice's first real
    payment: its txn_uid, whether its narration carries an invoice reference, its value date
    and known_at. That lets scoring explain which injected credits bite instead of assuming."""
    from matsya_platform.match.references import spellings
    from datetime import datetime, time
    r, rate = rng(cfg.seed, "coincidental_credit"), cfg.rate("coincidental_credit")
    empty = pl.DataFrame(schema={"truth_txn_uid": pl.String, "invoice_id": pl.String, "amount_paise": pl.Int64,
                                 "value_date": pl.Date, "tds_rate": pl.Float64, "tds_basis": pl.String, "real_txn_uid": pl.String, "real_has_reference": pl.Boolean,
                                 "real_value_date": pl.Date, "real_known_at": pl.Datetime("us", "UTC")})
    if rate <= 0:
        return [], {}, empty
    name = dict(zip(buyers["buyer_id"], buyers["name"]))
    tds_rate = dict(zip(truth_buyers["buyer_id"], truth_buyers["tds_rate"])) if truth_buyers is not None else {}
    first_paid: dict[str, object] = {}
    first_txn: dict[str, str] = {}
    if truth_payments is not None:
        for inv, d, t in truth_payments.sort("value_date", "txn_uid").group_by("invoice_id", maintain_order=True).agg(
                pl.col("value_date").first(), pl.col("txn_uid").first()).iter_rows():
            first_paid[inv], first_txn[inv] = d, t
    real = {} if truth_txns is None else {
        t: (n, k) for t, n, k in truth_txns.select("txn_uid", "narration_raw", "known_at").iter_rows()}
    rows, fids, truth = [], {}, []
    for inv in invoices.sort("issue_date", "invoice_id").iter_rows(named=True):
        draw, offset, minute = r.random(), r.random(), r.randrange(4 * 60, 13 * 60)
        if draw >= rate:
            continue
        paid = first_paid.get(inv["invoice_id"])
        latest = (paid - inv["issue_date"]).days - 1 if paid else window_days - 1
        latest = min(latest, window_days - 1)
        if latest < 1:
            continue  # the real payment comes too soon to be preceded
        d = inv["issue_date"] + timedelta(days=1 + int(offset * latest))
        known = datetime.combine(d, time(), tzinfo=cut.tzinfo) + timedelta(minutes=minute)
        if known > cut:
            continue
        gst = inv["gst_paise"] or 0
        rate_b = tds_rate.get(inv["buyer_id"], 0.0)
        base = inv["gross_paise"] if cfg.coincidental_basis == "gross" else inv["gross_paise"] - gst
        amount = inv["gross_paise"] - round(rate_b * base)
        bank_id = f"COINC{len(fids):05d}"
        uid = ids.txn_uid(inv["tenant_id"], template["account_id"], ids.txn_key_bank_id(bank_id, d, amount))
        row = {**{k: template[k] for k in ("tenant_id", "account_id", "source", "dq_score")},
               "txn_uid": uid, "value_date": d, "known_at": known, "amount_paise": amount, "direction": "credit",
               "narration_raw": f"BY NEFT/HDFCN{d:%y%j}{len(fids):08d}/{name[inv['buyer_id']]}/PAYMENT",
               "dup_group_id": None, "bank_txn_id": bank_id, "day_seq": 0, "rail": "NEFT"}
        rows.append(row)
        fids[uid] = f"coincidental_credit-{len(fids):05d}"
        rt = first_txn.get(inv["invoice_id"])
        narr, rknown = real.get(rt, (None, None))
        truth.append({"truth_txn_uid": uid, "invoice_id": inv["invoice_id"], "amount_paise": amount, "value_date": d,
                      "tds_rate": rate_b, "tds_basis": cfg.coincidental_basis,
                      "real_txn_uid": rt, "real_has_reference": bool(spellings(narr)) if rt else None,
                      "real_value_date": paid, "real_known_at": rknown})
    return rows, fids, pl.DataFrame(truth, schema=empty.schema) if truth else empty
