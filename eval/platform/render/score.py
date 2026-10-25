"""Run A4 over a rendered folder and compare with the expected tables.

Deliveries are built in manifest order, threading the ledger state. A ContinuityError on a
payload whose expected behaviour is "accept_gap" plays the operator: it records the
accept_gap written in the fault log (not the break A4 reports, so A4 cannot grade its own
homework) and rebuilds that payload once. Every A4 output is validated in full.

The invoice CSV, when rendered, is ingested with the generic CSV adapter against the
reference buyers the render copied, and compared with expected/ledger.invoices.

Report: per-table row match, rupees reconciled, each fault's expected vs observed
behaviour, and the named mismatches (known differences we report rather than hide).

The stress block judges the result against truth, independently of the expected tables.
Every A4 ledger row carries the (payload_id, index) it came from, which row_map traces to a
truth transaction. A discrepancy is loud when the pipeline flagged it (a continuity
failure on every payload carrying a missing row, a dup_group_id or lowered dq_score on an
extra row, continuity_ok false on a balance, a reject for a missing invoice) and silent
otherwise. A row dropped by an undetectable partial window counts as missing too: a correct
pipeline loses it as well, but only a loud one admits it. A payload A4 reports as
unverified (no currentBalance, no Summary) is a flag: rows missing from it are loud. Verdict per run, first match wins:
  SILENT   something is wrong and nothing says so (outranks every strict check);
  exact    every strict check passes;
  correct  not exact, but the ledger agrees with truth (timing or flag fields differ);
  loud     wrong, and every wrong thing is flagged: the correct way to fail.
"""

import json
import shutil
from collections import Counter
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from matsya_contracts.enums import ConsentStatus
from matsya_contracts.events import AcceptGap
from matsya_platform.ledger.build import LedgerState, build, validate_outputs
from matsya_platform.ledger.continuity import ContinuityError
from matsya_platform.ingest.invoices import BuyerDirectory, IngestResult, ingest
from matsya_platform.ingest.registry import adapter
from matsya_platform.ledger.rebit import Payload
from matsya_platform.match.buyers import CreditBuyerResolver
from matsya_platform.match.config import MatchConfig
from matsya_platform.match.engine import match as a6_match
from matsya_platform.store.export import export_contract_dir
from matsya_platform.store.persist import (latest_coverage, persist_build, persist_invoices, persist_match,
                                           persist_refs, read_build, read_match)
from matsya_platform.consent.service import ConsentService
from matsya_platform.fetch.adapter import LocalFolderAdapter
from matsya_platform.fetch.runner import fetch, stored_payload
from matsya_platform.store.keys import KeyStore
from matsya_platform.store.raw import RawStore
from matsya_platform.store.tables import TableStore

LOOP_CONSENT = "C-loop"

INVOICE_FIELDS = ("irn", "buyer_id", "issue_date", "due_date", "terms_days", "terms_source", "gross_paise",
                  "gst_paise", "expected_tds_paise", "status", "known_at")
TXN_FIELDS = ("account_id", "value_date", "known_at", "amount_paise", "direction", "narration_raw", "source",
              "dq_score", "dup_group_id")


def _payload(out: Path, d: dict) -> Payload:
    doc = json.loads((out / d["file"]).read_text())
    return Payload(tenant_id=d["tenant_id"], payload_id=d["payload_id"],
                   fetched_at=datetime.fromisoformat(d["fetched_at"]), doc=doc)


def _accept_from_log(fault_log: list[dict], payload_id: str) -> tuple[AcceptGap, ...]:
    out = []
    for f in fault_log:
        if f["kind"] == "partial_window" and f["mode"] == "accept_gap" and f["target"]["payload_id"] == payload_id:
            g = f["expected"]["accept_gap"]
            out.append(AcceptGap(account_id=f["target"]["account_id"], from_date=g["from_date"], to_date=g["to_date"],
                                 gap_paise=g["gap_paise"], accepted_by="score.py operator", note=f["fault_id"]))
    return tuple(out)


def services(store_dir: Path) -> ConsentService:
    """Consent service, encrypted raw store and encrypted table store over one folder, sharing
    one KeyStore (the master key comes from MATSYA_MASTER_KEY; unset, this fails loudly)."""
    keys = KeyStore(store_dir)
    return ConsentService(TableStore(store_dir, keys), keys, RawStore(store_dir, keys))


def open_consent(svc: ConsentService, manifest: dict, cut: datetime) -> str:
    """The run's consent, active from just before the first fetch until well after the cut."""
    ds = manifest["deliveries"]
    tenant, first = ds[0]["tenant_id"], min(datetime.fromisoformat(d["fetched_at"]) for d in ds)
    svc.create(tenant_id=tenant, consent_id=LOOP_CONSENT, fip_id="FIP-STUB", purpose="cash-flow forecast",
               range_from=min(date.fromisoformat(d["start"]) for d in ds),
               range_to=max(date.fromisoformat(d["end"]) for d in ds), frequency="weekly",
               expires_at=cut + timedelta(days=365), at=first - timedelta(minutes=1))
    svc.transition(tenant, LOOP_CONSENT, ConsentStatus.ACTIVE, first - timedelta(minutes=1))
    return LOOP_CONSENT


class _RevokesDuringFetch(LocalFolderAdapter):
    """The owner revokes while this fetch is in flight: the payload is in hand, then revoked."""

    def __init__(self, folder, svc: ConsentService, only: set[str], at: datetime):
        super().__init__(folder, only=only)
        self.svc, self.at = svc, at

    def fetch(self, consent, attempt):
        got = super().fetch(consent, attempt)
        self.svc.transition(consent.tenant_id, consent.consent_id, ConsentStatus.REVOKED, self.at)
        return got


def run_a4(out: Path, manifest: dict, fault_log: list[dict], svc: ConsentService | None = None,
           revoke_at: int | None = None) -> tuple[LedgerState, list[dict]]:
    """Every delivery, in order. With a consent service (the loop's path), each delivery is
    fetched through A2 under the run's active consent into the encrypted raw store, read back
    from there, built, and the build's outputs persisted (run_id = sequence and payload id).
    A repeat delivery is dropped at fetch by its payload hash. revoke_at revokes the consent
    during that delivery's fetch: its payload is discarded and nothing after it is fetched.
    Without a service, payloads are read straight from the folder (no persistence)."""
    state, log = LedgerState(), []
    for seq, d in enumerate(manifest["deliveries"]):
        entry = {"payload_id": d["payload_id"], "expect": d["expect"], "raised": None, "accepted_gap": None}
        if svc is None:
            p = _payload(out, d)
        else:
            tenant = d["tenant_id"]
            if svc.get(tenant, LOOP_CONSENT).status != ConsentStatus.ACTIVE:
                log.append({**entry, "not_fetched": "consent not active"})
                continue
            at = datetime.fromisoformat(d["fetched_at"])
            adapter = (_RevokesDuringFetch(out, svc, {d["payload_id"]}, at) if seq == revoke_at
                       else LocalFolderAdapter(out, only={d["payload_id"]}))
            commit = fetch(svc, adapter, tenant, LOOP_CONSENT, sleep=lambda s: None, now=lambda: at).commit
            if commit.discarded:
                log.append({**entry, "discarded": True})
                continue
            if commit.duplicates:
                log.append({**entry, "deduplicated": True, "duplicate_of": d.get("duplicate_of"), "new_txns": 0, "new_events": 0, "quarantined": 0,
                            "unverified": False, "new_bank_reported": 0, "new_reconstructed": 0})
                continue
            p = stored_payload(svc, tenant, d["payload_id"])
        try:
            r = build([p], state)
        except ContinuityError as e:
            entry["raised"] = [{"account_id": b.account_id, "on": b.on.isoformat(), "gap_paise": b.gap_paise}
                               for b in e.breaks]
            if d["expect"] != "accept_gap":
                entry["state_unchanged"] = True  # build is pure; the old state is kept
                log.append(entry)
                continue
            gaps = _accept_from_log(fault_log, d["payload_id"])
            entry["accepted_gap"] = [{"on": g.from_date.isoformat(), "gap_paise": g.gap_paise} for g in gaps]
            try:
                r = build([p], replace(state, accepted_gaps=state.accepted_gaps + gaps))
            except ContinuityError as e2:
                entry["raised_after_accept"] = [{"on": b.on.isoformat(), "gap_paise": b.gap_paise} for b in e2.breaks]
                log.append(entry)
                continue
        validate_outputs(r, sample=None)
        if svc is not None:
            persist_build(svc.tables, r, d["tenant_id"], f"{seq:05d}:{d['payload_id']}")
        entry.update(new_txns=r.txns.height, new_events=r.events.height, quarantined=r.quarantine.height,
                     unverified=r.unverified.height > 0,
                     new_bank_reported=r.balances.filter(pl.col("balance_source") == "bank_reported").height,
                     new_reconstructed=r.balances.filter(pl.col("balance_source") == "reconstructed").height)
        state = r.state
        log.append(entry)
    return state, log


def _compare_txns(actual: pl.DataFrame, expected: pl.DataFrame, row_map: pl.DataFrame) -> dict:
    a, e = actual.select("txn_uid", *TXN_FIELDS), expected.select("txn_uid", *TXN_FIELDS)
    j = e.join(a, on="txn_uid", how="inner", suffix="_a")
    field_mismatch = {}
    for f in TXN_FIELDS:
        n = j.filter(~(pl.col(f).eq_missing(pl.col(f"{f}_a")))).height
        if n:
            field_mismatch[f] = n
    exact = j.filter(pl.all_horizontal(pl.col(f).eq_missing(pl.col(f"{f}_a")) for f in TXN_FIELDS))
    mapped = set(row_map["expected_txn_uid"])
    return {
        "expected": e.height, "actual": a.height, "matched": j.height, "exact": exact.height,
        "missing": e.height - j.height, "extra": a.join(e, on="txn_uid", how="anti").height,
        "field_mismatch": field_mismatch,
        "row_map_uids_in_ledger": len(mapped & set(a["txn_uid"])), "row_map_uids": len(mapped),
        "rupees": {
            "expected_abs_paise": int(e["amount_paise"].abs().sum()),
            "reconciled_abs_paise": int(exact["amount_paise"].abs().sum()),
            "expected_net_paise": int(e["amount_paise"].sum()), "actual_net_paise": int(a["amount_paise"].sum()),
        },
    }


def _latest(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort("known_at").group_by("account_id", "date", "balance_source", maintain_order=True).last()


def _compare_balances(actual: pl.DataFrame, expected: pl.DataFrame) -> dict:
    keys = ["account_id", "date", "known_at", "closing_paise"]
    ab, eb = (x.filter(pl.col("balance_source") == "bank_reported").select(keys) for x in (actual, expected))
    bank = {"expected": eb.height, "actual": ab.height, "matched": eb.join(ab, on=keys, how="semi").height}
    ar = _latest(actual.filter(pl.col("balance_source") == "reconstructed"))
    er = expected.filter(pl.col("balance_source") == "reconstructed")
    j = er.join(ar, on=["account_id", "date"], how="left", suffix="_a")
    same = (pl.col("closing_paise") == pl.col("closing_paise_a")) & pl.col("continuity_ok").eq_missing(
        pl.col("continuity_ok_a")) & (pl.col("known_at") == pl.col("known_at_a"))
    bad = j.filter(~same.fill_null(False))
    # Same figure and flag, different known_at: A4 knew it at another fetch than the model
    # predicts (an accepted gap fills the closing before the missing row arrives). Reported,
    # not counted as a mismatch.
    figure_ok = (pl.col("closing_paise") == pl.col("closing_paise_a")) & pl.col("continuity_ok").eq_missing(
        pl.col("continuity_ok_a"))
    known_at_only = bad.filter(figure_ok.fill_null(False)).height
    bad = bad.filter(~figure_ok.fill_null(False))
    # A miss explained by contract F1: the bank row for that date shares the known_at.
    f1 = bad.join(expected.filter(pl.col("balance_source") == "bank_reported").select("account_id", "date", "known_at"),
                  on=["account_id", "date", "known_at"], how="semi")
    return {"bank_reported": bank,
            "reconstructed": {"expected": er.height, "matched": er.height - bad.height,
                              "mismatch_f1_collision": f1.height, "mismatch_other": bad.height - f1.height,
                              "known_at_only": known_at_only,
                              "extra_dates": ar.join(er, on=["account_id", "date"], how="anti").height}}


@dataclass(frozen=True)
class InvoiceIngest:
    invoices: pl.DataFrame  # every file's new ledger.invoices rows
    by_format: dict[str, IngestResult]  # each file's own result; its source rows are per file

    def tagged(self, attr: str) -> list[tuple[str, object]]:
        return [(f, x) for f, r in self.by_format.items() for x in getattr(r, attr)]


def _ingest_invoices(out: Path, manifest: dict) -> InvoiceIngest:
    """Each invoice file through the adapter its manifest entry names, in order, each seeing
    the invoice ids the earlier files produced as already in the ledger."""
    m = manifest["invoices"]
    ref = {n: pl.read_parquet(out / "ref" / f"{n}.parquet") for n in ("ref.buyers", "ref.tenant_buyers")
           if (out / "ref" / f"{n}.parquet").exists()}
    buyers = BuyerDirectory(ref["ref.buyers"], ref.get("ref.tenant_buyers"), m["tenant_id"])
    seen: frozenset[str] = frozenset()
    results = {}
    for f in m["files"]:
        r = ingest((out / f["file"]).read_bytes(), adapter(f["adapter"]), tenant_id=m["tenant_id"],
                   supplier_gstin=m["supplier_gstin"], received_at=datetime.fromisoformat(m["received_at"]),
                   buyers=buyers, existing_invoice_ids=seen)
        results[f["format"]] = r
        seen = seen | frozenset(r.invoices["invoice_id"])
    return InvoiceIngest(pl.concat([r.invoices for r in results.values()]), results)


def _compare_invoices(result: InvoiceIngest, expected: pl.DataFrame, rmap: pl.DataFrame) -> dict:
    a = result.invoices.select("invoice_id", *INVOICE_FIELDS)
    e = expected.select("invoice_id", *INVOICE_FIELDS)
    j = e.join(a, on="invoice_id", how="inner", suffix="_a")
    eq = [pl.col(f).eq_missing(pl.col(f"{f}_a")) for f in INVOICE_FIELDS]
    exact = j.filter(pl.all_horizontal(eq))
    methods = dict(sorted(Counter(x.method for _, x in result.tagged("resolutions")).items()))
    rejects = dict(sorted(Counter(x.reason for _, x in result.tagged("rejects")).items()))
    fmt_of = dict(rmap.filter(pl.col("duplicate_of_row").is_null()).select("invoice_id", "format").iter_rows())
    exact_ids = set(exact["invoice_id"])
    by_format = {}
    for fmt, r in result.by_format.items():
        mine = {i for i, f in fmt_of.items() if f == fmt}
        got = r.invoices
        by_format[fmt] = {
            "invoices_rendered": len(mine), "expected": e.filter(pl.col("invoice_id").is_in(list(mine))).height,
            "actual": got.height, "exact": len(exact_ids & mine),
            "rejects": dict(sorted(Counter(x.reason for x in r.rejects).items())),
            "duplicates": len(r.duplicates),
            "resolution": dict(sorted(Counter(x.method for x in r.resolutions).items())),
            "terms_source": dict(sorted(Counter(got["terms_source"]).items())),
            "with_irn": got.filter(pl.col("irn").is_not_null()).height,
        }
    return {"expected": e.height, "actual": a.height, "matched": j.height, "exact": exact.height, "by_format": by_format,
            "missing": e.height - j.height, "extra": a.join(e, on="invoice_id", how="anti").height,
            "field_mismatch": {f: n for f in INVOICE_FIELDS
                               if (n := j.filter(~pl.col(f).eq_missing(pl.col(f"{f}_a"))).height)},
            "gross_expected_paise": int(e["gross_paise"].sum()), "gross_reconciled_paise": int(exact["gross_paise"].sum()),
            "resolution_methods": methods, "rejects": rejects, "duplicates": len(result.tagged("duplicates"))}


def _invoice_faults(fault_log: list[dict], result: InvoiceIngest) -> list[dict]:
    dups = {(f, d.source_row): d for f, d in result.tagged("duplicates")}
    res = {(f, x.source_row): x for f, x in result.tagged("resolutions")}
    rej = {(f, x.source_row): x for f, x in result.tagged("rejects")}
    ledger = set(result.invoices["invoice_id"])
    out = []
    for f in fault_log:
        t, x = f.get("target", {}), f["expected"]
        if f["kind"] == "duplicate_row":
            d = dups.get((t["format"], t["source_row"]))
            got = {"reported_duplicate": d is not None, "identical": d.identical if d else None,
                   "first_row": d.first_row if d else None, "ledger_rows": int(t["invoice_id"] in ledger)}
            ok = got["ledger_rows"] == x["ledger_rows"] and (
                x["ledger_rows"] == 0 or (got["identical"] is True and got["first_row"] == t["duplicate_of_row"]))
        elif f["kind"] == "missing_gstin":
            r, j = res.get((t["format"], t["source_row"])), rej.get((t["format"], t["source_row"]))
            got = {"method": r.method if r else None, "buyer_id": r.buyer_id if r else None,
                   "reject": j.reason if j else None}
            ok = (got["reject"] == x["reject"]) if "reject" in x else (
                got["method"] == x["method"] and got["buyer_id"] == x["buyer_id"])
        else:
            continue
        out.append({"fault_id": f["fault_id"], "kind": f["kind"], "pass": bool(ok), "expected": x, "observed": got})
    return out


def _faults(fault_log: list[dict], run_log: list[dict], state: LedgerState, manifest: dict,
            ledger: pl.DataFrame) -> list[dict]:
    """ledger: ledger.transactions as read back from the store; state only for row lineage."""
    by_pid = {e["payload_id"]: e for e in run_log}
    fetched = {d["payload_id"]: datetime.fromisoformat(d["fetched_at"]) for d in manifest["deliveries"]}
    uids = set(ledger["txn_uid"])
    path_of = {r.txn_uid: r.key_path for r in state.rows}
    out = []
    for f in fault_log:
        x, got, ok = f["expected"], {}, False
        if f["kind"] in ("duplicate_row", "missing_gstin"):
            continue
        if f["kind"] == "duplicate_payload":
            e = by_pid[f["target"]["duplicate_payload_id"]]
            got = {k: e.get(k) for k in x}
            ok = e["raised"] is None and got == x
        elif f["kind"] in ("drop_txn_id", "drop_all_ids", "drop_balances", "identical_rows", "coincidental_credit"):
            want, truth = f["target"]["expected_txn_uid"], f["target"]["truth_txn_uid"]
            got = {"expected_uid_in_ledger": want in uids, "truth_uid_in_ledger": truth in uids,
                   "key_path": path_of.get(want)}
            ok = got["expected_uid_in_ledger"] and (want == truth or not got["truth_uid_in_ledger"]) \
                and got["key_path"] == x["key_path"]
        elif f["kind"] == "partial_window" and not x["detectable"]:
            e = by_pid[f["target"]["payload_id"]]
            got = {"raised": e["raised"]}
            ok = e["raised"] is None  # nothing can see it; the stress block reports the lost row
        elif f["kind"] == "partial_window":
            e = by_pid[f["target"]["payload_id"]]
            raised = e["raised"] or []
            gap_ok = any(b["gap_paise"] == x["gap_paise"] and b["on"] == x["break_on"] for b in raised)
            row = ledger.filter(pl.col("txn_uid") == f["target"]["dropped_expected_txn_uid"])
            got = {"raised": raised, "dropped_row_recovered": row.height == 1,
                   "recovered_known_at": row["known_at"][0].isoformat() if row.height else None}
            ok = gap_ok and got["dropped_row_recovered"] and got["recovered_known_at"] == x["recovered_known_at"]
            if e.get("raised_after_accept"):
                got["raised_after_accept"] = e["raised_after_accept"]
                ok = False
            if f["mode"] == "batch_fails":
                got["state_unchanged"] = e.get("state_unchanged", False)
                ok = ok and got["state_unchanged"]
            else:
                got["accepted_gap"] = e["accepted_gap"]
                at = fetched[f["target"]["payload_id"]]
                got["rows_with_lowered_dq"] = ledger.filter((pl.col("dq_score") < 1) & (pl.col("known_at") == at)).height
                ok = ok and e["accepted_gap"] is not None and got["rows_with_lowered_dq"] == x["rows_with_lowered_dq"]
        out.append({"fault_id": f["fault_id"], "kind": f["kind"], "pass": bool(ok), "expected": x, "observed": got})
    return out


def _stress(state: LedgerState, bal: pl.DataFrame, run_log: list[dict], row_map: pl.DataFrame, reference: pl.DataFrame,
            meta: dict,
            inv: dict | None, inv_silent: int) -> dict:
    keys = pl.DataFrame([{"payload_id": r.payload_id, "row_index": r.index, "txn_uid": r.txn_uid,
                          "value_date_a": r.value_date, "amount_a": r.amount_paise,
                          "narration_a": (r.txn.narration_raw or "").strip(), "flagged": r.txn.dup_group_id is not None
                          or r.txn.dq_score < 1, "known_at": r.known_at, "key_path": r.key_path} for r in state.rows],
                        schema={"payload_id": pl.String, "row_index": pl.Int32, "txn_uid": pl.String,
                                "value_date_a": pl.Date, "amount_a": pl.Int64, "narration_a": pl.String,
                                "flagged": pl.Boolean, "known_at": pl.Datetime("us", "UTC"), "key_path": pl.String})
    held = keys.join(row_map.select("payload_id", "row_index", "truth_txn_uid", "account_id", "value_date",
                                    "amount_paise", "narration"), on=["payload_id", "row_index"], how="left")
    unmapped = held.filter(pl.col("truth_txn_uid").is_null()).height
    wrong_facts = held.filter(pl.col("truth_txn_uid").is_not_null() & (
        (pl.col("value_date") != pl.col("value_date_a")) | (pl.col("amount_paise") != pl.col("amount_a"))
        | (pl.col("narration") != pl.col("narration_a")))).height
    dup = held.filter(pl.col("truth_txn_uid").is_not_null()).with_columns(
        pl.len().over("truth_txn_uid").alias("copies"), pl.col("flagged").any().over("truth_txn_uid").alias("any_flag"))
    extra = dup.filter(pl.col("copies") > 1).group_by("truth_txn_uid").agg(
        (pl.len() - 1).alias("n"), pl.col("any_flag").first())
    extra_loud = int(extra.filter(pl.col("any_flag"))["n"].sum() or 0)
    extra_silent = int(extra.filter(~pl.col("any_flag"))["n"].sum() or 0)

    failed = {e["payload_id"] for e in run_log if e["raised"] and not (e["accepted_gap"] and not e.get("raised_after_accept"))}
    raised = {e["payload_id"] for e in run_log if e["raised"]}
    unverified = {e["payload_id"] for e in run_log if e.get("unverified")}
    flagged_payloads = failed | unverified
    # A delivery dropped at fetch as a repeat is the same bytes as its original: same flags.
    flagged_payloads |= {e["payload_id"] for e in run_log if e.get("deduplicated") and e.get("duplicate_of") in flagged_payloads}
    carried = dict(row_map.group_by("truth_txn_uid").agg(pl.col("payload_id").unique()).iter_rows())
    held_ids = set(held["truth_txn_uid"].drop_nulls())
    missing_loud = missing_silent = 0
    for uid, omitted_in in reference.iter_rows():
        if uid in held_ids:
            continue
        pids = set(carried.get(uid, []))
        if (pids and pids <= flagged_payloads) or (not pids and (omitted_in in raised or omitted_in in unverified)):
            missing_loud += 1
        else:
            missing_silent += 1

    # Balances: is each reconstructed closing consistent with the rows A4 holds?
    distinct = dup.group_by("truth_txn_uid").agg(pl.col("account_id").first(), pl.col("value_date").first(),
                                                pl.col("amount_paise").first(), pl.col("known_at").min())
    own = (dup.group_by("account_id", "value_date").agg(pl.col("amount_a").sum().alias("flow_a"),
                                                       pl.col("known_at").max().alias("ka"))
           .join(distinct.group_by("account_id", "value_date").agg(pl.col("amount_paise").sum().alias("flow")),
                 on=["account_id", "value_date"], how="full", coalesce=True)
           .sort("account_id", "value_date")
           .with_columns(pl.col("flow").fill_null(0), pl.col("ka").cum_max().over("account_id")))
    own = own.with_columns((pl.col("account_id").replace_strict(meta["opening"], return_dtype=pl.Int64)
                            + pl.col("flow").cum_sum().over("account_id")).alias("ref"))
    rec = bal.filter(pl.col("balance_source") == "reconstructed").sort("known_at").group_by(
        "account_id", "date", maintain_order=True).last()
    bank = bal.filter(pl.col("balance_source") == "bank_reported").select("account_id", "date", "known_at")
    j = rec.join(own.rename({"value_date": "date"}), on=["account_id", "date"], how="left")
    bad = j.filter((pl.col("closing_paise") != pl.col("ref")).fill_null(True))
    claimed_ok = bad.filter(pl.col("continuity_ok").fill_null(False))
    f1 = claimed_ok.join(bank, left_on=["account_id", "date", "ka"], right_on=["account_id", "date", "known_at"],
                         how="semi").height
    balance_silent = claimed_ok.height - f1
    balance_loud = bad.height - claimed_ok.height

    paths = dict(keys.group_by("key_path").len().iter_rows())
    silent = unmapped + wrong_facts + extra_silent + missing_silent + balance_silent + inv_silent
    loud = extra_loud + missing_loud + balance_loud + (sum(inv["rejects"].values()) if inv else 0)
    return {"silent_total": silent, "loud_total": loud, "unmapped_rows": unmapped, "wrong_facts": wrong_facts,
            "extra_rows_silent": extra_silent, "extra_rows_flagged": extra_loud, "missing_rows_silent": missing_silent,
            "missing_rows_loud": missing_loud, "balances_silent": balance_silent, "balances_flagged": balance_loud,
            "balances_f1": f1, "invoices_silent": inv_silent,
            "continuity_failures": sum(1 for e in run_log if e["raised"]),
            "quarantined_rows": sum(e.get("quarantined", 0) for e in run_log),
            "unverified_payloads": len(unverified),
            "key_paths": {p: paths.get(p, 0) for p in ("txn", "utr", "bal", "ord")}, "ledger_rows": keys.height,
            "truth_rows": row_map["truth_txn_uid"].n_unique()}


def _invoice_silent(inv_result, expected: pl.DataFrame, inv_rmap: pl.DataFrame) -> int:
    """Invoice discrepancies nobody flagged: extra rows, missing rows that were not rejected,
    and wrong business facts (the CSV path's modelled fields are left out)."""
    facts = ("buyer_id", "issue_date", "due_date", "terms_days", "terms_source", "gross_paise", "gst_paise")
    a = inv_result.invoices.select("invoice_id", *facts)
    e = expected.select("invoice_id", *facts)
    j = e.join(a, on="invoice_id", how="inner", suffix="_a")
    wrong = j.filter(~pl.all_horizontal(pl.col(f).eq_missing(pl.col(f"{f}_a")) for f in facts)).height
    extra = a.join(e, on="invoice_id", how="anti").height
    rejected_rows = {(f, x.source_row) for f, x in inv_result.tagged("rejects")}
    rejected = {i for f, n, i in inv_rmap.select("format", "source_row", "invoice_id").iter_rows()
                if (f, n) in rejected_rows}
    missing = e.join(a, on="invoice_id", how="anti")
    return wrong + extra + missing.filter(~pl.col("invoice_id").is_in(list(rejected))).height


def _truth_uid_of(state: LedgerState, row_map: pl.DataFrame) -> dict[str, str]:
    """A4 ledger txn_uid -> truth txn_uid, through the (payload_id, index) each row came from."""
    rm = {(p, i): t for p, i, t in row_map.select("payload_id", "row_index", "truth_txn_uid").iter_rows()}
    return {r.txn_uid: rm.get((r.payload_id, r.index)) for r in state.rows}


def _baseline(tx: pl.DataFrame, invoices: pl.DataFrame, buyers: pl.DataFrame, tenant_buyers, tenant_id: str,
              as_of: datetime, cfg: MatchConfig) -> pl.DataFrame:
    """Exact-gross baseline: a credit from a resolved buyer links to the one open invoice of
    that buyer, inside the window, whose gross equals the credit within tolerance."""
    res = CreditBuyerResolver(buyers, tenant_buyers, tenant_id, cfg.treds_platforms)
    inv = sorted(invoices.iter_rows(named=True), key=lambda r: (r["issue_date"], r["invoice_id"]))
    used, out = set(), []
    for c in tx.filter((pl.col("amount_paise") > 0) & (pl.col("known_at") <= as_of)).sort(
            "value_date", "known_at", "txn_uid").iter_rows(named=True):
        b = res.resolve(c["narration_raw"]).buyer_id
        if b is None:
            continue
        fits = [i for i in inv if i["buyer_id"] == b and i["invoice_id"] not in used
                and 0 <= (c["value_date"] - i["issue_date"]).days <= cfg.window_days
                and abs(i["gross_paise"] - c["amount_paise"]) <= cfg.tolerance_paise]
        if len(fits) == 1:
            used.add(fits[0]["invoice_id"])
            out.append({"txn_uid": c["txn_uid"], "invoice_id": fits[0]["invoice_id"], "allocated_paise": c["amount_paise"],
                        "tds_rate": 0.0, "tds_basis": None, "payer_kind": "buyer", "value_date": c["value_date"]})
    return pl.DataFrame(out, schema={"txn_uid": pl.String, "invoice_id": pl.String, "allocated_paise": pl.Int64,
                                     "tds_rate": pl.Float64, "tds_basis": pl.String, "payer_kind": pl.String,
                                     "value_date": pl.Date})


def _m(value, n):
    return {"value": value, "n": n}


def _pct(xs: list[int], q: float) -> float:
    return float(xs[min(len(xs) - 1, int(q * len(xs)))]) if xs else 0.0


def _metrics(links: pl.DataFrame, paid: dict[str, object], truth: dict, review_share, unexplained,
             no_label: set[str]) -> dict:
    """links: txn_uid (A4), invoice_id, allocated_paise, tds_rate, tds_basis, payer_kind.
    paid: invoice_id -> predicted paid_date (None if open). no_label: invoices given no label.

    Paid-date error is over every invoice truth says is paid by the cut (TReDS discounts
    excluded: their label is censored, not paid). An invoice with a predicted date
    contributes |predicted - truth| days; one without (missed link, review item with no
    label, or an open label) contributes (cut - truth paid date): how long the label leaves
    it looking unpaid."""
    to_truth, gold = truth["to_truth"], truth["links"]
    pred = {(to_truth.get(t), i): r for t, i, *r in links.select(
        "txn_uid", "invoice_id", "allocated_paise", "tds_rate", "tds_basis", "payer_kind").iter_rows()}
    good = set(pred) & set(gold)
    total = sum(g["amount_paise"] for g in gold.values())
    alloc = sum(min(pred[k][0], gold[k]["amount_paise"]) for k in good)
    dated, full, false_paid, no_date_label, no_date_open = [], [], 0, 0, 0
    for inv, t in truth["paid"].items():
        p = paid.get(inv)
        if t is None:
            false_paid += p is not None
            continue
        if p is not None:
            dated.append(abs((p - t).days))
            full.append(dated[-1])
        else:
            full.append((truth["cut"] - t).days)
            if inv in no_label:
                no_date_label += 1
            else:
                no_date_open += 1
    dated.sort()
    full.sort()
    n_paid = len(full)
    rates = [(pred[k][1], truth["rate"][gold[k]["buyer_id"]]) for k in good if pred[k][1] is not None]
    bases = [pred[k][2] for k in good if pred[k][2] is not None]
    kinds = [(pred[k][3], gold[k]["payer_kind"]) for k in good]
    treds = truth["treds_invoices"]
    return {
        "precision": _m(len(good) / len(pred) if pred else 0.0, len(pred)),
        "recall": _m(len(good) / len(gold) if gold else 0.0, len(gold)),
        "rupees_allocated_share": _m(alloc / total if total else 0.0, len(gold)),
        "paid_truth": _m(n_paid, n_paid),
        "paid_err_full_mean": _m(sum(full) / n_paid if n_paid else 0.0, n_paid),
        "paid_err_full_p50": _m(_pct(full, 0.5), n_paid), "paid_err_full_p95": _m(_pct(full, 0.95), n_paid),
        "paid_err_full_max": _m(float(full[-1]) if full else 0.0, n_paid),
        "paid_err_dated_mean": _m(sum(dated) / len(dated) if dated else 0.0, len(dated)),
        "paid_err_dated_p95": _m(_pct(dated, 0.95), len(dated)),
        "paid_no_date_open_label": _m(no_date_open, n_paid), "paid_no_date_no_label": _m(no_date_label, n_paid),
        "false_paid": _m(false_paid, len(truth["paid"])),
        "treds_labelled": _m(sum(1 for i in treds if truth["kind_pred"].get(i) == "treds_discount"), len(treds))
        if truth.get("kind_pred") is not None else None,
        "tds_rate_accuracy": _m(sum(1 for p, t in rates if abs(p - t) < 1e-9) / len(rates) if rates else 0.0, len(rates)),
        "tds_basis_accuracy": _m(sum(1 for b in bases if b == "taxable_value") / len(bases) if bases else 0.0,
                                 len(bases)),
        "payer_kind_accuracy": _m(sum(1 for p, t in kinds if p == t) / len(kinds) if kinds else 0.0, len(kinds)),
        "review_share": review_share, "unexplained_rupees": unexplained,
    }


def _coincidental(truth_dir: Path, r, to_truth: dict, gold: dict, pays: pl.DataFrame) -> dict | None:
    """What happened to injected non-invoice credits, and to the real payments they pre-empted."""
    p = truth_dir / "coincidental.parquet"
    pred = {(to_truth.get(t), i) for t, i in r.links.select("txn_uid", "invoice_id").iter_rows()}
    wrong = pred - set(gold)
    if not p.exists():
        cause_of = {to_truth.get(i.txn_uid): i.reason.split(":")[0] for i in r.review}
        return {"injected": 0, "wrong_links": len(wrong), "wrong_from_fault": 0,
                "real_flagged_by_basis": sum(1 for c in cause_of.values() if c == "basis evidence")}
    inj = pl.read_parquet(p)
    inj_ids = set(inj["truth_txn_uid"])
    in_review = {to_truth.get(i.txn_uid) for i in r.review}
    cause_of = {to_truth.get(i.txn_uid): i.reason.split(":")[0] for i in r.review}
    linked = {t for t, _ in pred}
    stolen = {i for t, i in pred if t in inj_ids}
    # Fate of each real payment of an invoice an injected credit was linked to.
    fate = {"linked_to_its_invoice": 0, "linked_to_another_invoice": 0, "review": 0, "unmatched": 0}
    for row in pays.filter(pl.col("invoice_id").is_in(list(stolen))).iter_rows(named=True):
        t = row["txn_uid"]
        if t not in set(to_truth.values()):
            continue  # not yet delivered at the cut
        if (t, row["invoice_id"]) in pred:
            fate["linked_to_its_invoice"] += 1
        elif t in linked:
            fate["linked_to_another_invoice"] += 1
        elif t in in_review:
            fate["review"] += 1
        else:
            fate["unmatched"] += 1
    return {"injected": len(inj_ids & set(to_truth.values())), "injected_linked": len(inj_ids & linked),
            "injected_in_review": len(inj_ids & in_review), "wrong_links": len(wrong),
            "wrong_from_fault": sum(1 for t, _ in wrong if t in inj_ids), "stolen_invoices": len(stolen),
            "real_payment_fate": fate, "alerts": 0, "bite_by_cause": _bite(inj, to_truth, pred),
            "injected_review_causes": dict(sorted(Counter(cause_of[t] for t in inj_ids if t in cause_of).items())),
            "injected_zero_tds": inj.filter(pl.col("truth_txn_uid").is_in(list(set(to_truth.values())))
                                            & (pl.col("tds_rate") == 0)).height,
            "real_flagged_by_basis": sum(1 for t, c in cause_of.items() if c == "basis evidence" and t not in inj_ids)}


def _bite(inj: pl.DataFrame, to_truth: dict, pred: set) -> list[dict]:
    """Which injected credits got linked to the invoice they mimic, split by what was true of
    that invoice's real payment at the cut: delivered or not, and with or without a reference."""
    held = set(to_truth.values())
    linked = {t for t, _ in pred}
    cells: dict[tuple, list[int]] = {}
    for row in inj.iter_rows(named=True):
        if row["truth_txn_uid"] not in held:
            continue
        if row["real_txn_uid"] is None:
            key = ("no real payment", None)
        elif row["real_txn_uid"] not in held:
            key = ("real payment not delivered by the cut", None)
        else:
            key = ("real payment delivered", row["real_has_reference"])
        cell = cells.setdefault(key, [0, 0])
        cell[0] += 1
        cell[1] += row["truth_txn_uid"] in linked
    return [{"real_payment": k[0], "real_has_reference": k[1], "injected": v[0], "linked": v[1]}
            for k, v in sorted(cells.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]


def _categories(truth_dir: Path, to_truth: dict, tenant_id: str) -> pl.DataFrame | None:
    """features.txn_category stand-in from truth (see cli._truth_categories)."""
    p = truth_dir / "categories.parquet"
    if not p.exists():
        return None
    cat = dict(pl.read_parquet(p).iter_rows())
    rows = [{"tenant_id": tenant_id, "txn_uid": a, "category": cat.get(t, "abstain"), "confidence": 0.9,
             "source": "model", "needs_llm": False} for a, t in to_truth.items() if t]
    return pl.DataFrame(rows, schema={"tenant_id": pl.String, "txn_uid": pl.String, "category": pl.String,
                                      "confidence": pl.Float64, "source": pl.String, "needs_llm": pl.Boolean})


def _matching(out: Path, manifest: dict, state: LedgerState, row_map: pl.DataFrame, inv_result, meta: dict,
              store: TableStore, match_overrides: dict | None = None) -> dict | None:
    """Run A6 on what A4 and the invoice ingest stored, persist its output, and score what is
    read back against Track B's truth. state supplies only row lineage for the truth mapping."""
    truth_dir = out / "truth"
    if not (truth_dir / "payments.parquet").exists() or inv_result is None:
        return None
    m = manifest["invoices"]
    cut = datetime.fromisoformat(meta["cut"])
    tenant = m["tenant_id"]
    ref = {"ref.buyers": store.read("ref.buyers"), "ref.tenant_buyers": store.read("ref.tenant_buyers", tenant)}
    cov = latest_coverage(store.read("aux.coverage", tenant))
    through = cov["complete_through"].min() if cov.height else cut.date()
    tx = store.read("ledger.transactions", tenant)
    invoices = store.read("ledger.invoices", tenant)
    cfg = MatchConfig(**(match_overrides or {}))
    to_truth = _truth_uid_of(state, row_map)
    cats = _categories(truth_dir, to_truth, m["tenant_id"]) if cfg.category_gate else None
    r = a6_match(tx, invoices, ref["ref.buyers"], ref.get("ref.tenant_buyers"), tenant_id=m["tenant_id"],
                 supplier_gstin=m["supplier_gstin"], as_of=cut, complete_through=through, config=cfg,
                 txn_category=cats)
    persist_match(store, r, tenant, "match-0")
    stored = read_match(store, tenant)
    links = stored["links"].drop("run_id", "tenant_id")

    held = {v for v in to_truth.values() if v}
    ingested = set(invoices["invoice_id"])
    pays = pl.read_parquet(truth_dir / "payments.parquet")
    tinv = pl.read_parquet(truth_dir / "invoices.parquet")
    tb = pl.read_parquet(truth_dir / "buyers.parquet")
    buyer_of = dict(zip(tinv["invoice_id"], tinv["buyer_id"]))
    gold = {(p["txn_uid"], p["invoice_id"]): {**p, "buyer_id": buyer_of.get(p["invoice_id"])}
            for p in pays.iter_rows(named=True) if p["txn_uid"] in held and p["invoice_id"] in ingested}
    # Truth paid date at the cut: every payment of the invoice held, and enough of them to settle it.
    by_inv: dict[str, list] = {}
    for p in pays.iter_rows(named=True):
        by_inv.setdefault(p["invoice_id"], []).append(p)
    paid_truth, treds = {}, []
    for t in tinv.filter(pl.col("invoice_id").is_in(list(ingested))).iter_rows(named=True):
        if t["treds_discount_date"] is not None and any(p["txn_uid"] in held for p in by_inv.get(t["invoice_id"], [])):
            treds.append(t["invoice_id"])  # censored at the discount date, not a paid date
            continue
        ps = [p for p in by_inv.get(t["invoice_id"], []) if p["txn_uid"] in held]
        settled = sum(p["amount_paise"] + p["tds_paise"] for p in ps)
        paid_truth[t["invoice_id"]] = max(p["value_date"] for p in ps)             if ps and settled >= t["gross_paise"] - cfg.tolerance_paise else None
    excluded = r.excluded_invoices
    truth = {"to_truth": to_truth, "links": gold, "rate": dict(zip(tb["buyer_id"], tb["tds_rate"])),
             "paid": paid_truth, "treds_invoices": treds, "cut": cut.date(),
             "kind_pred": dict(zip(stored["outcomes"]["invoice_id"], stored["outcomes"]["settlement_kind"]))}
    paid_pred = {o["invoice_id"]: (None if o["settlement_kind"] == "treds_discount" else o["paid_date"])
                 for o in stored["outcomes"].iter_rows(named=True)}
    considered = r.stats["credits_considered"]
    a6 = _metrics(links, paid_pred, truth, _m(len(r.review) / considered if considered else 0.0, considered),
                  _m(r.stats["unexplained_credit_paise"] / 100, considered), excluded)

    base = _baseline(tx, invoices, ref["ref.buyers"], ref.get("ref.tenant_buyers"), m["tenant_id"], cut, cfg)
    base_paid = {}
    for row in base.iter_rows(named=True):
        base_paid[row["invoice_id"]] = row["value_date"]
    truth_b = {**truth, "paid": paid_truth, "kind_pred": None}
    base_alloc = int(base["allocated_paise"].sum() or 0)
    b = _metrics(base, {i: base_paid.get(i) for i in paid_truth}, truth_b, None,
                 _m((r.stats["considered_credit_paise"] - base_alloc) / 100, considered), set())
    stages = dict(r.links.group_by("stage").len().sort("stage").iter_rows())
    a6["coincidental"] = _coincidental(truth_dir, r, to_truth, gold, pays)
    a6["contradiction_events"] = _m(len(r.events), considered)
    a6["review_causes"] = dict(sorted(Counter(i["cause"] for i in r.review_ranked).items()))
    a6["review_coverage"] = r.review_coverage
    a6["review_groups"] = _m(len(r.review_groups), len(r.review_ranked))
    a6["credits_gated_by_category"] = _m(r.stats["credits_gated_by_category"], r.stats["credits"])
    a6["label_evidence"] = dict(sorted(Counter(r.label_evidence.values()).items()))
    return {"a6": a6, "baseline": b, "a6_stats": r.stats, "a6_stages": stages,
            "review_examples": [{"txn_uid": i.txn_uid, "amount_paise": i.amount_paise, "reason": i.reason,
                                 "candidates": [{"invoice_ids": c.invoice_ids, "stage": c.stage,
                                                 "unexplained_paise": c.unexplained_paise} for c in i.candidates[:3]]}
                                for i in r.review[:5]]}


def score(out_dir: str | Path, match_overrides: dict | None = None) -> dict:
    out = Path(out_dir)
    manifest = json.loads((out / "manifest.json").read_text())
    fault_log = json.loads((out / "fault_log.json").read_text())["faults"]
    meta = json.loads((out / "expected" / "meta.json").read_text())
    row_map = pl.read_parquet(out / "row_map.parquet")
    exp = {n: pl.read_parquet(out / "expected" / f"{n}.parquet")
           for n in ("ledger.transactions", "ledger.balances", "ledger.coverage", "reference_rows")}

    # Everything scored below is what the store holds after the run, not in-memory frames.
    store_dir = out / "store"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    svc = services(store_dir)
    store = svc.tables
    if manifest["deliveries"]:
        open_consent(svc, manifest, datetime.fromisoformat(meta["cut"]))
    state, run_log = run_a4(out, manifest, fault_log, svc)
    tenant = manifest["deliveries"][0]["tenant_id"] if manifest["deliveries"] else None
    stored = read_build(store, tenant)
    stored_txns = stored["txns"].sort("txn_uid")
    unexpected = [e for e in run_log if e["raised"] and e["expect"] == "ok"]
    txns = _compare_txns(stored_txns, exp["ledger.transactions"], row_map)
    bal = _compare_balances(stored["balances"], exp["ledger.balances"])
    cov_a = latest_coverage(stored["coverage"]).select("account_id", "complete_through")
    cov_e = exp["ledger.coverage"].select("account_id", "complete_through")
    faults = _faults(fault_log, run_log, state, manifest, stored_txns)
    inv, inv_silent, result = None, 0, None
    if "invoices" in manifest:
        result = _ingest_invoices(out, manifest)
        mi = manifest["invoices"]
        persist_refs(store, pl.read_parquet(out / "ref" / "ref.buyers.parquet"),
                     pl.read_parquet(out / "ref" / "ref.tenant_buyers.parquet")
                     if (out / "ref" / "ref.tenant_buyers.parquet").exists() else None, mi["tenant_id"])
        persist_invoices(store, result.invoices, mi["tenant_id"])
        # Compared and matched from here on: the invoices as stored, not the ingest's frame.
        result = replace(result, invoices=store.read("ledger.invoices", mi["tenant_id"]))
        exp_inv = pl.read_parquet(out / "expected" / "ledger.invoices.parquet")
        inv_rmap = pl.read_parquet(out / "invoices" / "row_map.parquet")
        inv = _compare_invoices(result, exp_inv, inv_rmap)
        inv_silent = _invoice_silent(result, exp_inv, inv_rmap)
        faults += _invoice_faults(fault_log, result)
        expected_rejects = len(meta["invoices"]["expected_rejects"])

    checks = {
        "no_unexpected_failures": not unexpected,
        "transactions_exact": txns["exact"] == txns["expected"] and txns["extra"] == 0,
        "rupees_reconciled": txns["rupees"]["reconciled_abs_paise"] == txns["rupees"]["expected_abs_paise"]
        and txns["rupees"]["expected_net_paise"] == txns["rupees"]["actual_net_paise"],
        "row_map_complete": txns["row_map_uids_in_ledger"] == txns["row_map_uids"],
        "bank_reported_exact": bal["bank_reported"]["matched"] == bal["bank_reported"]["expected"]
        == bal["bank_reported"]["actual"],
        "reconstructed_explained": bal["reconstructed"]["mismatch_other"] == 0
        and bal["reconstructed"]["extra_dates"] == 0,
        "coverage_equal": cov_a.sort("account_id").equals(cov_e.sort("account_id")),
        "faults_behaved": all(f["pass"] for f in faults),
    }
    if inv is not None:
        checks["invoices_exact"] = inv["exact"] == inv["expected"] and inv["extra"] == 0
        checks["invoice_gross_reconciled"] = inv["gross_reconciled_paise"] == inv["gross_expected_paise"]
        checks["no_unexpected_invoice_rejects"] = sum(inv["rejects"].values()) == expected_rejects
    warnings = []
    if meta.get("cut_inferred", True):
        warnings.append(f"cut inferred from the latest known_at in the world ({meta['cut']}); "
                        "the export does not record as_of yet")
    stress = _stress(state, stored["balances"], run_log, row_map, exp["reference_rows"], meta, inv, inv_silent)
    passed = all(checks.values())
    truth_ok = stress["silent_total"] == 0 and stress["loud_total"] == 0 and txns["rupees"]["expected_net_paise"] \
        == txns["rupees"]["actual_net_paise"]
    stress["verdict"] = ("SILENT" if stress["silent_total"] else "exact" if passed else
                         "correct" if truth_ok else "loud")
    stress["failed_checks"] = [k for k, v in checks.items() if not v]
    stress["rupees_reconciled_share"] = (txns["rupees"]["reconciled_abs_paise"] / txns["rupees"]["expected_abs_paise"]
                                         if txns["rupees"]["expected_abs_paise"] else 1.0)
    stress["transactions_exact_share"] = txns["exact"] / txns["expected"] if txns["expected"] else 1.0
    stress["invoices_exact_share"] = (inv["exact"] / inv["expected"] if inv and inv["expected"] else None)
    report = {
        "warnings": warnings, "pass": passed, "checks": checks, "deliveries": len(run_log), "stress": stress,
        "unexpected_failures": unexpected, "tables": {"ledger.transactions": txns, "ledger.balances": bal,
                                                      "ledger.coverage": {"actual": cov_a.rows(), "expected": cov_e.rows()}},
        "faults": faults,
        "named_mismatches": {
            "f1_balances_key_collision": bal["reconstructed"]["mismatch_f1_collision"],
            "identity_collisions": meta.get("identity_collisions", 0),
            "undetectable_partials": meta.get("undetectable_partials", 0),
            "reconstructed_known_at_only": bal["reconstructed"]["known_at_only"],
            "unfetched_at_cut": meta["unfetched_at_cut"], "ist_boundary_rows": meta["ist_boundary_rows"],
            "watermark_vs_coverage": meta.get("watermark_vs_coverage"),
            "invoice_ingest_lag": meta["invoice_ingest_lag"], "treds": meta["treds"],
        },
    }
    report["matching"] = _matching(out, manifest, state, row_map, result, meta, store, match_overrides)
    report["export"] = export_contract_dir(store, out / "contract")
    if inv is not None:
        report["tables"]["ledger.invoices"] = inv
        report["named_mismatches"]["invoices"] = {k: v for k, v in meta["invoices"].items() if k != "invoice_ingest_lag"}
    (out / "score.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report
