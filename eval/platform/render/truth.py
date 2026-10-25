"""Expected outcomes for a rendered world: row_map, fault_log, expected contract tables.

Expected tables follow our rules, not the generator's:
- known_at is the fetched_at of the first payload expected to succeed that carries the row;
- cut dates are IST dates;
- a reconstructed balance exists only for dates with transactions; bank_reported rows come
  from each delivered payload's Summary;
- censored_at for open labels is A4's coverage (complete_through), not the max-lag
  watermark; the difference is reported as a named mismatch.
Expected txn_uids follow A4's key path order for what each payload actually carried
(txnId, UTR/RRN reference, running balance, ordinal), built with the contracts ids.txn_key_*
functions; a row's expected id is the one from its first successful delivery.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import polars as pl

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame, validate
from render.aa_rebit import PayloadOut
from render.csv_generic import issue_versions
from render.faults import PartialPlan
from render.windows import Window, ist_date
from render.world import World

TREDS_MARKERS = ("RXIL", "M1XCHANGE", "INVOICEMART")
INVOICE_INGEST_LAG_NOTE = ("Invoices are known at issue in the generator; Tally/CSV ingest lag is not "
                           "simulated, so the invoice side of the loop is easier than reality.")


@dataclass
class Delivery:
    payload_id: str
    window: Window
    fetched_at: datetime
    payload: PayloadOut
    expect: str  # "ok" | "fail" | "accept_gap"
    payload_fault_id: str | None = None
    duplicate_of: str | None = None
    partial: PartialPlan | None = None
    observed_fail: bool = False  # closed loop: A4 actually refused it

    @property
    def counts(self) -> bool:
        """Whether the delivery reaches the ledger (expected to succeed and not refused)."""
        return self.expect != "fail" and not self.observed_fail

    @property
    def doc(self) -> dict:
        return self.payload.doc

    @property
    def order(self) -> list[str]:
        return self.payload.order

    @property
    def summary_paise(self) -> int:
        return self.payload.summary_paise


def keyed(d: Delivery, by_uid: dict[str, dict]) -> list[tuple[str, str, str]]:
    """(truth txn_uid, expected txn_uid, key path) per rendered row, choosing the path by our
    rules: txnId, then reference (UTR/RRN, once per reference and amount per payload), then
    running balance, then the ordinal among identical rows of this payload."""
    seen: Counter = Counter()
    utrs: set = set()
    out = []
    for rr in d.payload.rows:
        r = by_uid[rr.txn_uid]
        vd, amt, narr = r["value_date"], r["amount_paise"], (r["narration_raw"] or "").strip()
        utr_free = bool(rr.reference) and (r["account_id"], rr.reference, amt) not in utrs
        if rr.txn_id:
            path, key = "txn", ids.txn_key_bank_id(rr.txn_id, vd, amt)
        elif utr_free:
            utrs.add((r["account_id"], rr.reference, amt))
            path, key = "utr", ids.txn_key_utr(rr.reference, amt)
        elif rr.balance_after_paise is not None:
            path, key = "bal", ids.txn_key_balance(rr.balance_after_paise, vd, amt)
        else:
            ident = (r["account_id"], vd, amt, ids.normalise_party(narr))
            path, key = "ord", ids.txn_key_ordinal(vd, amt, narr, seen[ident])
            seen[ident] += 1
        out.append((rr.txn_uid, ids.txn_uid(r["tenant_id"], r["account_id"], key), path))
    return out


def first_deliveries(deliveries: list[Delivery]) -> dict[str, Delivery]:
    """truth txn_uid -> the first delivery expected to succeed that carries it."""
    first: dict[str, Delivery] = {}
    for d in deliveries:
        if d.counts:
            for uid in d.order:
                first.setdefault(uid, d)
    return first


def row_map(deliveries: list[Delivery], rows: pl.DataFrame, row_faults: dict[str, str]) -> pl.DataFrame:
    by_uid = {r["txn_uid"]: r for r in rows.iter_rows(named=True)}
    out = []
    for d in deliveries:
        for i, (truth_uid, exp_uid, path) in enumerate(keyed(d, by_uid)):
            r = by_uid[truth_uid]
            out.append({"payload_id": d.payload_id, "row_index": i, "expected_txn_uid": exp_uid, "key_path": path,
                        "truth_txn_uid": truth_uid, "fault_id": row_faults.get(truth_uid) or d.payload_fault_id,
                        "payload_fault_id": d.payload_fault_id, "account_id": r["account_id"],
                        "value_date": r["value_date"], "amount_paise": r["amount_paise"],
                        "narration": (r["narration_raw"] or "").strip()})
    return pl.DataFrame(out, schema={"payload_id": pl.String, "row_index": pl.Int32, "expected_txn_uid": pl.String,
                                     "key_path": pl.String, "truth_txn_uid": pl.String, "fault_id": pl.String,
                                     "payload_fault_id": pl.String, "account_id": pl.String, "value_date": pl.Date,
                                     "amount_paise": pl.Int64, "narration": pl.String})


def expected_tables(world: World, rows: pl.DataFrame, deliveries: list[Delivery], opening: dict[str, int],
                    reference: pl.DataFrame) -> tuple[dict[str, pl.DataFrame], dict]:
    """reference: every truth row a correct pipeline should end up holding (truth_txn_uid,
    omitted_in), including rows a partial window dropped and nothing re-delivered."""
    by_uid = {r["txn_uid"]: r for r in rows.iter_rows(named=True)}
    ok = [d for d in deliveries if d.counts]
    first_seen = first_deliveries(deliveries)
    exp_uid = {}
    for d in ok:
        for truth_uid, uid, _ in keyed(d, by_uid):
            if first_seen[truth_uid] is d:
                exp_uid[truth_uid] = uid

    txns = []
    for r in rows.iter_rows(named=True):
        d = first_seen.get(r["txn_uid"])
        if d is None:
            continue
        p = d.partial
        posted = ist_date(r["known_at"])
        unverified = all(x.balance_after_paise is None for x in d.payload.rows) and d.summary_paise is None
        capped = (p is not None and p.mode == "accept_gap" and p.detectable and posted == p.break_on) or unverified
        txns.append(T.Transaction(
            tenant_id=r["tenant_id"], txn_uid=exp_uid[r["txn_uid"]], account_id=r["account_id"],
            value_date=r["value_date"], known_at=d.fetched_at, amount_paise=r["amount_paise"],
            direction=r["direction"], narration_raw=(r["narration_raw"] or "").strip() or None, source="aa",
            dq_score=0.5 if capped else 1.0, dup_group_id=None))
    tx = frame(T.Transaction, txns).sort("account_id", "value_date", "txn_uid")
    # Two distinct truth rows can map to one expected id (identical rows keyed by UTR or
    # by an ordinal that shifted between payloads). The contract cannot hold both under
    # that id, so the expected table keeps the first; the truth-based check in score.py
    # still sees the other as a lost row.
    collisions = tx.height - tx["txn_uid"].n_unique()
    tx = tx.unique("txn_uid", keep="first", maintain_order=True)
    validate(tx, T.Transaction)

    bank = frame(T.Balance, [T.Balance(
        tenant_id=world.tables["ledger.transactions"]["tenant_id"][0], account_id=d.window.account_id,
        date=ist_date(d.window.end_ts), closing_paise=d.summary_paise, continuity_ok=None,
        balance_source="bank_reported", known_at=d.fetched_at) for d in ok if d.summary_paise is not None])
    validate(bank, T.Balance)
    daily = (tx.group_by("tenant_id", "account_id", "value_date")
             .agg(pl.col("amount_paise").sum().alias("flow"), pl.col("known_at").max().alias("ka"))
             .sort("account_id", "value_date")
             .with_columns(pl.col("ka").cum_max().over("account_id"),
                           (pl.col("account_id").replace_strict(opening, return_dtype=pl.Int64)
                            + pl.col("flow").cum_sum().over("account_id")).alias("closing_paise")))
    recon = daily.select("tenant_id", "account_id", pl.col("value_date").alias("date"), "closing_paise",
                         pl.lit(True).alias("continuity_ok"), pl.lit("reconstructed").alias("balance_source"),
                         pl.col("ka").alias("known_at")).cast(bank.schema)
    validate(recon, T.Balance)
    # Contract F1: the two sources can share (tenant, account, date, known_at), so the
    # expected table is checked per source and written as one file without a key check.
    balances = pl.concat([bank, recon]).sort("account_id", "date", "balance_source", "known_at")
    f1 = recon.join(bank, on=["tenant_id", "account_id", "date", "known_at"], how="semi").height

    cov_rows = []
    for acc in sorted({d.window.account_id for d in ok}):
        mine = [d for d in ok if d.window.account_id == acc]
        cov_rows.append({"tenant_id": tx["tenant_id"][0] if tx.height else None, "account_id": acc,
                         "complete_through": max(d.window.end for d in mine),
                         "known_at": max(d.fetched_at for d in mine)})
    coverage = pl.DataFrame(cov_rows, schema={"tenant_id": pl.String, "account_id": pl.String,
                                              "complete_through": pl.Date, "known_at": pl.Datetime("us", "UTC")})

    out = {"ledger.transactions": tx, "ledger.balances": balances, "ledger.coverage": coverage,
           "reference_rows": reference}
    meta = {"cut": world.cut.isoformat(), "last_fetch": max(d.fetched_at for d in ok).isoformat(), "opening": opening,
            "f1_predicted_collisions": f1, "identity_collisions": collisions,
            "undetectable_partials": sum(1 for d in deliveries if d.partial is not None and not d.partial.detectable),
            "invoice_ingest_lag": INVOICE_INGEST_LAG_NOTE}

    world_tx = world.tables["ledger.transactions"]
    unfetched = world_tx.filter(~pl.col("txn_uid").is_in(list(first_seen)))
    meta["unfetched_at_cut"] = {"rows": unfetched.height,
                                "why": "known by the generator's cut but after the last scheduled fetch"}
    ist = world_tx.with_columns(pl.col("known_at").dt.convert_time_zone("Asia/Kolkata").dt.date().alias("ist"))
    meta["ist_boundary_rows"] = {
        "rows": ist.filter(pl.col("ist") != pl.col("known_at").dt.date()).height,
        "why": "known_at between 18:30 and 24:00 UTC: the IST date is the next day, so UTC dating would put "
               "these rows in a different window"}

    if "labels.invoice_outcomes" in world.tables and coverage.height:
        labels, wm = _labels_with_coverage(world.tables["labels.invoice_outcomes"], coverage["complete_through"].min())
        out["labels.invoice_outcomes"] = labels
        meta["watermark_vs_coverage"] = wm
    meta["treds"] = _treds(world, rows, first_seen)
    return out, meta


def _labels_with_coverage(labels: pl.DataFrame, complete_through) -> tuple[pl.DataFrame, dict]:
    latest = labels.with_columns(
        (pl.col("known_at") == pl.col("known_at").max().over("tenant_id", "invoice_id")).alias("_latest"))
    open_latest = pl.col("_latest") & pl.col("paid_date").is_null()
    theirs = latest.filter(open_latest)["censored_at"]
    rebuilt = latest.with_columns(
        pl.when(open_latest).then(pl.lit(complete_through)).otherwise(pl.col("censored_at")).alias("censored_at")
    ).drop("_latest")
    validate(rebuilt, T.InvoiceOutcome)
    diffs = [(complete_through - c).days for c in theirs if c is not None]
    return rebuilt, {
        "name": "watermark_vs_coverage", "open_labels": len(theirs),
        "differ": sum(1 for x in diffs if x != 0),
        "generator_censored_at": sorted({c.isoformat() for c in theirs if c is not None}),
        "coverage_censored_at": complete_through.isoformat(),
        "days_coverage_minus_generator": sorted(set(diffs)),
    }


def _treds(world: World, rows: pl.DataFrame, first_seen: dict) -> dict:
    rendered = rows.filter(pl.col("txn_uid").is_in(list(first_seen)))
    platform = rendered.filter(pl.col("narration_raw").str.contains("|".join(TREDS_MARKERS)))
    out = {"platform_credits_rendered": platform.height}
    if world.truth_payments is not None:
        kinds = platform.join(world.truth_payments.select("txn_uid", "payer_kind"), on="txn_uid", how="left")
        out["pass_through"] = kinds.filter(pl.col("payer_kind") == "buyer").height
        out["discount"] = kinds.filter(pl.col("payer_kind") == "treds_financier").height
    m, inv, lab = (world.tables.get(n) for n in ("ledger.matches", "ledger.invoices", "labels.invoice_outcomes"))
    if m is not None and inv is not None and lab is not None:
        disc = m.filter(pl.col("payer_kind") == "treds_financier").group_by("invoice_id").agg(
            (pl.col("allocated_paise") + pl.col("tds_inferred_paise")).sum().alias("settled"))
        gross = inv.group_by("invoice_id").agg(pl.col("gross_paise").first())
        last = lab.sort("known_at").group_by("invoice_id", maintain_order=True).last()
        chk = disc.join(gross, on="invoice_id").join(last, on="invoice_id")
        out["discounts_short_of_gross"] = chk.filter(pl.col("settled") < pl.col("gross_paise")).height
        out["discounts_settled_in_expected_labels"] = chk.filter(
            (pl.col("settlement_kind") == "treds_discount") & pl.col("paid_date").is_not_null()).height
        out["discount_invoices"] = chk.height
    out["matcher_assertion"] = "pending A6: no matcher yet, so pass-through vs discount is checked on expected labels only"
    return out


def expected_invoices(world: World, received_at: datetime, rmap: pl.DataFrame, duplicate_rows: dict[str, str],
                      missing_gstin: dict[str, str]) -> tuple[pl.DataFrame, list[dict], dict]:
    """Expected ledger.invoices after ingesting the rendered CSV, the invoice faults' log
    entries, and named mismatches against the generator's table.

    Expected rows are the issue-time versions with what the invoice files can know: irn as
    the e-invoice renderer wrote it (null in CSV and day book), expected_tds_paise null (no
    matcher), status open, known_at = when the files arrived. An invoice that can only be
    resolved by name (a missing_gstin CSV row, or any day book voucher, which shows no GSTIN)
    is expected to be rejected when its buyer's name is not unique."""
    inv = world.tables["ledger.invoices"]
    buyers = world.tables["ref.buyers"]
    counts = buyers.group_by(pl.col("name").map_elements(ids.normalise_party, return_dtype=pl.String)).len()
    unique_names = set(counts.filter(pl.col("len") == 1)["name"])
    name_of = dict(zip(buyers["buyer_id"], buyers["name"]))
    first = issue_versions(inv)
    fmt_of = dict(rmap.filter(pl.col("duplicate_of_row").is_null()).select("invoice_id", "format").iter_rows())
    irn_of = dict(rmap.filter(pl.col("irn").is_not_null()).select("invoice_id", "irn").unique().iter_rows())
    by_name = set(missing_gstin) | {i for i, f in fmt_of.items() if f == "tally_daybook"}
    buyer_of = dict(first.select("invoice_id", "buyer_id").iter_rows())
    rejected = {i for i in by_name if ids.normalise_party(name_of[buyer_of[i]]) not in unique_names}
    exp = first.filter(~pl.col("invoice_id").is_in(list(rejected))).with_columns(
        pl.col("invoice_id").replace_strict(irn_of, default=None, return_dtype=pl.String).alias("irn"),
        pl.lit(None, pl.Int64).alias("expected_tds_paise"),
        pl.lit("open").alias("status"), pl.lit(received_at).cast(pl.Datetime("us", "UTC")).alias("known_at"),
    ).select(list(T.Invoice.model_fields)).sort("invoice_id")
    validate(exp, T.Invoice)

    log = []
    for r in rmap.filter(pl.col("fault_id").is_not_null()).iter_rows(named=True):
        if r["fault_id"].startswith("duplicate_row"):
            log.append({"fault_id": r["fault_id"], "kind": "duplicate_row",
                        "target": {"invoice_id": r["invoice_id"], "format": r["format"], "source_row": r["source_row"],
                                   "duplicate_of_row": r["duplicate_of_row"]},
                        "expected": {"ledger_rows": 0 if r["invoice_id"] in rejected else 1,
                                     "duplicate_identical": r["invoice_id"] not in rejected}})
        elif r["fault_id"].startswith("missing_gstin"):
            buyer = first.filter(pl.col("invoice_id") == r["invoice_id"])["buyer_id"][0]
            log.append({"fault_id": r["fault_id"], "kind": "missing_gstin",
                        "target": {"invoice_id": r["invoice_id"], "format": r["format"], "source_row": r["source_row"]},
                        "expected": ({"reject": "unresolved_buyer"} if r["invoice_id"] in rejected
                                     else {"method": "name", "buyer_id": buyer})})
    meta = {
        "invoice_ingest_lag": INVOICE_INGEST_LAG_NOTE,
        "irn_in_world_not_rendered": first.filter(pl.col("irn").is_not_null()
                                                  & ~pl.col("invoice_id").is_in(list(irn_of))).height,
        "invoices_by_format": dict(sorted(Counter(fmt_of.values()).items())),
        "expected_tds_not_inferred": first.filter(pl.col("expected_tds_paise").is_not_null()).height,
        "later_versions_not_ingested": {"rows": inv.height - first.height,
                                        "why": "status changes come from the matcher, which is not built yet"},
        "expected_rejects": sorted(rejected),
    }
    return exp, log, meta
