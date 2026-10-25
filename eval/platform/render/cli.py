"""Render a world folder to an output folder, score A4 on it, both, or run a stress sweep.

    python -m render.cli render WORLD_DIR OUT_DIR [--seed N] [--cadence-days 7] [--overlap-days 3]
                                [--partial-mode batch_fails|accept_gap] [--rate FAULT=SHARE ...]
    python -m render.cli score OUT_DIR
    python -m render.cli loop WORLD_DIR OUT_DIR [same options]
    python -m render.cli sweep WORLD_DIR[,WORLD_DIR...] [--levels 0,5,10,25,50,75,100] [--seeds 0,1,2]
                               [--faults all|name,name] [--modes batch_fails,accept_gap]
                               [--out-dir eval/platform/outputs]

score, loop and sweep keep the run in an encrypted store and need MATSYA_MASTER_KEY set (see
.env.example); without it they stop with an error.

Output folder (render):
    manifest.json            render config and deliveries in delivery order
    payloads/<id>.json       ReBIT AA payloads
    invoices/invoices.csv    owner-style invoice CSV
    invoices/daybook.csv     Tally-style day book (format as understood, not verified)
    invoices/einvoices.json  GST e-invoice documents, a small share (format as understood, not verified)
    invoices/row_map.parquet (format, source_row) -> invoice_id, fault_id, irn
    ref/*.parquet            the reference tables Track A already holds (buyers)
    fault_log.json           every fault with its expected pipeline behaviour
    row_map.parquet          (payload_id, row_index) -> expected txn_uid, key path -> fault_id
    expected/*.parquet       expected contract tables under our rules; meta.json
    score.json               written by score
    store/                   written by score: encrypted raw payloads, tables, wrapped keys
    contract/                written by score: plaintext <namespace>.<table>.parquet for Track B
"""

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import polars as pl

from render import aa_rebit, csv_generic, faults, gst_einvoice, tally_daybook, truth, windows
from render.world import World, load


class _ClosedLoopA4:
    """Runs A4 on each delivery as it is rendered, so A2 can react (closed loop)."""

    def __init__(self):
        from matsya_platform.ledger.build import LedgerState
        self.state = LedgerState()

    def deliver(self, d: "truth.Delivery", tenant: str) -> bool:
        from dataclasses import replace as dc_replace

        from matsya_contracts.events import AcceptGap
        from matsya_platform.ledger.build import build
        from matsya_platform.ledger.continuity import ContinuityError
        from matsya_platform.ledger.rebit import Payload
        p = Payload(tenant_id=tenant, payload_id=d.payload_id, fetched_at=d.fetched_at, doc=d.doc)
        try:
            r = build([p], self.state)
        except ContinuityError:
            plan = d.partial
            if not (plan and plan.detectable and plan.mode == "accept_gap"):
                return False
            gap = AcceptGap(account_id=d.window.account_id, from_date=plan.break_on, to_date=plan.break_on,
                            gap_paise=plan.amount_paise, accepted_by="closed-loop operator", note=plan.fault_id)
            try:
                r = build([p], dc_replace(self.state, accepted_gaps=self.state.accepted_gaps + (gap,)))
            except ContinuityError:
                return False
        self.state = r.state
        return True


def render(world_dir: str | Path, out_dir: str | Path, wcfg: windows.WindowConfig, fcfg: faults.FaultConfig,
           world: World | None = None, closed_loop: bool = False,
           invoice_mix: dict[str, float] | None = None) -> Path:
    """closed_loop: A2 re-fetches from coverage after any failed fetch, as observed by running
    A4 during rendering. Open loop (default) can only predict failures from the fault plan,
    so a fetch that fails unexpectedly is not re-fetched and later fetches keep failing:
    loud levels in open loop are partly a harness artifact."""
    world = world or load(world_dir)
    opening = world.opening()
    base = windows.with_posting(world.rows())
    coinc_truth = None
    if fcfg.rate("coincidental_credit") > 0 and "ledger.invoices" in world.tables:
        tmpl = {**base.row(0, named=True)}
        rows_c, coinc_faults, coinc_truth = faults.plan_coincidental(
            csv_generic.issue_versions(world.tables["ledger.invoices"]), world.tables["ref.buyers"],
            world.truth_payments, world.truth_buyers, world.cut, tmpl, fcfg, truth_txns=world.truth_txns)
        if rows_c:
            base = windows.with_posting(pl.concat([base, pl.DataFrame(rows_c, schema=world.rows().schema)
                                                   .with_columns(pl.lit(None, pl.Date).alias("posted_ist"))
                                                   .select(base.columns)]))
    else:
        coinc_faults = {}
    clones, clone_faults = faults.plan_clones(base, fcfg)  # identical_rows: real extra transactions
    rows = aa_rebit.with_running_balance(windows.with_posting(pl.concat([base, clones])), opening)
    uids = rows["txn_uid"].to_list()
    plans = {f: faults.plan_rows(uids, f, fcfg) for f in ("drop_txn_id", "drop_all_ids", "drop_balances")}
    drops = aa_rebit.IdDrops(txn_id=frozenset(plans["drop_txn_id"]), all_ids=frozenset(plans["drop_all_ids"]),
                             balances=frozenset(plans["drop_balances"]))
    row_faults: dict[str, str] = {**coinc_faults, **clone_faults}
    for plan in plans.values():
        for uid, fid in plan.items():
            row_faults[uid] = f"{row_faults[uid]}+{fid}" if uid in row_faults else fid

    tenant = world.tables["ledger.transactions"]["tenant_id"][0]
    a4 = _ClosedLoopA4() if closed_loop else None
    deliveries: list[truth.Delivery] = []
    reference: dict[str, str | None] = {}  # truth uid -> payload that omitted it (partial_window)
    for acc in sorted(set(rows["account_id"])):
        acc_rows = rows.filter(pl.col("account_id") == acc)
        origin = acc_rows["posted_ist"].min()
        ends = windows.end_dates(origin, world.cut, wcfg)
        r_dup, n_dup = faults.rng(fcfg.seed, "duplicate_payload"), [0]

        def emit(w, p, has_summary, acc=acc, acc_rows=acc_rows, r_dup=r_dup, n_dup=n_dup) -> bool:
            w_rows = windows.rows_in(w, acc_rows)
            for uid in w_rows["txn_uid"]:
                reference.setdefault(uid, None)
            if p is not None:
                reference[p.txn_uid] = w.payload_id
            before = acc_rows.filter(pl.col("known_at") <= w.end_ts)
            carried = int(before["balance_after_paise"][-1]) if before.height else opening[acc]
            out_ = aa_rebit.payload_doc(w, w_rows, carried, drops, {p.txn_uid} if p else set(), summary=has_summary)
            expect = ("ok" if p is None or not p.detectable else
                      "fail" if p.mode == "batch_fails" else "accept_gap")
            d = truth.Delivery(w.payload_id, w, w.fetched_at, out_, expect,
                               payload_fault_id=p.fault_id if p else None, partial=p)
            dup = None
            if r_dup.random() < fcfg.rate("duplicate_payload") and p is None:
                dup = truth.Delivery(f"{w.payload_id}-dup", w, w.fetched_at + timedelta(hours=1), out_, "ok",
                                     payload_fault_id=f"duplicate_payload-{n_dup[0]:04d}", duplicate_of=w.payload_id)
                n_dup[0] += 1
            ok = expect != "fail"
            if a4 is not None:
                ok = a4.deliver(d, tenant)
                d.observed_fail = not ok
                if dup is not None:
                    dup.observed_fail = not a4.deliver(dup, tenant)
            deliveries.extend(x for x in (d, dup) if x is not None)
            return ok

        faults.plan_windows(acc, acc_rows, origin, ends, wcfg, fcfg, drops.keeps_balance, outcome=emit)
    deliveries.sort(key=lambda d: (d.fetched_at, d.window.account_id, d.payload_id))

    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    (out / "payloads").mkdir(parents=True)
    (out / "expected").mkdir()
    manifest = {"world": str(Path(world_dir)), "cut": world.cut.isoformat(), "window_config": asdict(wcfg),
                "fault_config": asdict(fcfg), "closed_loop": closed_loop, "deliveries": []}
    for d in deliveries:
        f = f"payloads/{d.payload_id}.json"
        (out / f).write_text(json.dumps(d.doc, indent=2) + "\n")
        manifest["deliveries"].append({
            "payload_id": d.payload_id, "tenant_id": tenant, "account_id": d.window.account_id,
            "fetched_at": d.fetched_at.isoformat(), "file": f, "start": d.window.start.isoformat(),
            "end": d.window.end.isoformat(), "rows": len(d.order), "expect": d.expect,
            "payload_fault_id": d.payload_fault_id, "duplicate_of": d.duplicate_of,
            "observed_fail": d.observed_fail})
    ref = pl.DataFrame({"truth_txn_uid": list(reference), "omitted_in": list(reference.values())},
                       schema={"truth_txn_uid": pl.String, "omitted_in": pl.String})
    expected, meta = truth.expected_tables(world, rows, deliveries, opening, ref)
    rmap = truth.row_map(deliveries, rows, row_faults)
    inv_log = []
    if "ledger.invoices" in world.tables:
        inv_log, meta["invoices"] = _render_invoices(world, out, fcfg, manifest, invoice_mix)
    meta["cut_inferred"] = world.cut_inferred
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rmap.write_parquet(out / "row_map.parquet")
    for name, df in expected.items():
        df.write_parquet(out / "expected" / f"{name}.parquet")
    (out / "expected" / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    (out / "truth").mkdir()
    for name, df in (("payments", world.truth_payments), ("invoices", world.truth_invoices),
                     ("buyers", world.truth_buyers)):
        if df is not None:  # for scoring the matcher against the generator's own links
            df.write_parquet(out / "truth" / f"{name}.parquet")
    if coinc_truth is not None:  # injected non-invoice credits: never a link in truth
        coinc_truth.write_parquet(out / "truth" / "coincidental.parquet")
    _truth_categories(world, rows, coinc_faults).write_parquet(out / "truth" / "categories.parquet")
    log = _fault_log(deliveries, rmap, row_faults, expected["ledger.transactions"]) + inv_log
    (out / "fault_log.json").write_text(json.dumps({"faults": log}, indent=2, default=str) + "\n")
    return out


def _truth_categories(world, rows: pl.DataFrame, injected: dict) -> pl.DataFrame:
    """Every rendered row's true category, as a stand-in for Track B's categoriser in scoring.
    An injected coincidental credit is a customer_receipt here: its narration is identical to a
    payment's, so any narration-based categoriser would call it one. A clone (bank id + "D")
    takes its original's category."""
    by_bank = dict(zip(world.truth_txns["bank_txn_id"], world.truth_txns["category"]))
    cats = []
    for uid, bank in rows.select("txn_uid", "bank_txn_id").iter_rows():
        if uid in injected:
            cats.append("customer_receipt")
        else:
            cats.append(by_bank.get(bank) or by_bank.get(bank[:-1]) or "abstain")
    return pl.DataFrame({"truth_txn_uid": rows["txn_uid"], "category": cats})


# Share of invoices rendered in each format. E-invoices stay rare: micro suppliers below the
# e-invoicing turnover threshold have none. One supplier using two book formats is not
# realistic; it is how one render exercises all three adapters.
DEFAULT_INVOICE_MIX = {"gst_einvoice": 0.05, "tally_daybook": 0.45, "csv_generic": 0.50}
INVOICE_FILES = {"csv_generic": "invoices/invoices.csv", "tally_daybook": "invoices/daybook.csv",
                 "gst_einvoice": "invoices/einvoices.json"}


def _assign_formats(ids_: list[str], buyer_of: dict[str, str], gstins: dict[str, str | None], mix: dict[str, float],
                    seed: int) -> dict[str, str]:
    """invoice_id -> format, seeded. A buyer without a GSTIN cannot be on a B2B e-invoice."""
    r = faults.rng(seed, "invoice_formats")
    total = sum(mix.values())
    out = {}
    for i in ids_:
        u, acc, fmt = r.random() * total, 0.0, "csv_generic"
        for f, share in mix.items():
            acc += share
            if u < acc:
                fmt = f
                break
        if fmt == "gst_einvoice" and not gstins.get(buyer_of[i]):
            fmt = "csv_generic"
        out[i] = fmt
    return out


def _render_invoices(world, out: Path, fcfg: faults.FaultConfig, manifest: dict,
                     mix: dict[str, float] | None = None) -> tuple[list[dict], dict]:
    inv = world.tables["ledger.invoices"]
    buyers = world.tables["ref.buyers"]
    first = csv_generic.issue_versions(inv)
    ids_ = sorted(first["invoice_id"])
    fmt_of = _assign_formats(ids_, dict(zip(first["invoice_id"], first["buyer_id"])),
                             dict(zip(buyers["buyer_id"], buyers["gstin"])), mix or DEFAULT_INVOICE_MIX, fcfg.seed)
    dups = faults.plan_invoice_rows(ids_, "duplicate_row", fcfg)
    # Only the CSV carries a buyer GSTIN that can go missing: a day book never shows one and
    # a B2B e-invoice cannot omit it.
    miss = faults.plan_invoice_rows([i for i in ids_ if fmt_of[i] == "csv_generic"], "missing_gstin", fcfg)
    invoice_no = dict(zip(world.truth_invoices["invoice_id"], world.truth_invoices["invoice_no"]))
    sup = world.supplier_gstin()
    (out / "invoices").mkdir()
    files, maps = [], []
    for fmt, path in INVOICE_FILES.items():
        mine = [i for i in ids_ if fmt_of[i] == fmt]
        if not mine:
            continue
        sub = inv.filter(pl.col("invoice_id").is_in(mine))
        d = {i: f for i, f in dups.items() if i in set(mine)}
        if fmt == "csv_generic":
            data, rmap = csv_generic.render(sub, invoice_no, buyers, fcfg.seed, d, miss)
        elif fmt == "tally_daybook":
            data, rmap = tally_daybook.render(sub, invoice_no, buyers, fcfg.seed, d, sup)
        else:
            data, rmap = gst_einvoice.render(sub, invoice_no, buyers, fcfg.seed, d, sup)
        (out / path).write_bytes(data)
        files.append({"format": fmt, "adapter": fmt, "file": path, "rows": rmap.height, "invoices": len(mine)})
        maps.append(rmap.with_columns(pl.lit(fmt).alias("format")))
    rmap = pl.concat(maps, how="diagonal_relaxed")
    if "irn" not in rmap.columns:
        rmap = rmap.with_columns(pl.lit(None, pl.String).alias("irn"))
    rmap = rmap.select("format", "source_row", "invoice_id", "fault_id", "duplicate_of_row", "irn")
    rmap.write_parquet(out / "invoices" / "row_map.parquet")
    (out / "ref").mkdir()
    for name in ("ref.buyers", "ref.tenant_buyers"):
        if name in world.tables:
            world.tables[name].write_parquet(out / "ref" / f"{name}.parquet")
    received_at = world.cut  # one file, delivered at the cut
    exp, log, meta = truth.expected_invoices(world, received_at, rmap, dups, miss)
    exp.write_parquet(out / "expected" / "ledger.invoices.parquet")
    manifest["invoices"] = {"files": files, "tenant_id": inv["tenant_id"][0], "supplier_gstin": sup,
                            "received_at": received_at.isoformat(), "rows": rmap.height}
    return log, meta


def _fault_log(deliveries, rmap: pl.DataFrame, row_faults: dict[str, str], expected_tx) -> list[dict]:
    log = []
    for d in deliveries:
        if d.duplicate_of:
            log.append({"fault_id": d.payload_fault_id, "kind": "duplicate_payload",
                        "target": {"payload_id": d.duplicate_of, "duplicate_payload_id": d.payload_id},
                        # Byte-identical to the original, so the raw store's payload-hash check
                        # drops it at fetch: never stored, never built.
                        "expected": {"raised": None, "deduplicated": True, "new_txns": 0, "new_events": 0,
                                     "new_bank_reported": 0, "new_reconstructed": 0}})
    ok_pids = {d.payload_id for d in deliveries if d.counts}
    delivered = rmap.filter(pl.col("payload_id").is_in(list(ok_pids)))
    first = delivered.group_by("truth_txn_uid", maintain_order=True).first()
    by_truth = {r["truth_txn_uid"]: r for r in first.iter_rows(named=True)}
    for uid, fids in sorted(row_faults.items(), key=lambda kv: kv[1]):
        r = by_truth.get(uid)
        if r is None:
            continue
        for fid in fids.split("+"):
            log.append({"fault_id": fid, "kind": fid.rsplit("-", 1)[0],
                        "target": {"truth_txn_uid": uid, "expected_txn_uid": r["expected_txn_uid"]},
                        "expected": {"key_path": r["key_path"]}})
    for pd in (d for d in deliveries if d.partial is not None):
        p = pd.partial
        target = {"payload_id": pd.payload_id, "account_id": pd.window.account_id, "dropped_txn_uid": p.txn_uid,
                  "dropped_expected_txn_uid": (by_truth.get(p.txn_uid) or {}).get("expected_txn_uid"),
                  "amount_paise": p.amount_paise}
        if not p.detectable:
            log.append({"fault_id": p.fault_id, "kind": "partial_window", "mode": p.mode, "target": target,
                        "expected": {"detectable": False, "raises": None,
                                     "behaviour": "no anchor follows the dropped row: A4 cannot see the gap and "
                                                  "the row is lost"}})
            continue
        later = next((d for d in deliveries if d.fetched_at > pd.fetched_at and p.txn_uid in d.order
                      and d.expect != "fail" and not d.observed_fail), None)
        if later is None:  # closed loop can end before anything re-delivers it
            log.append({"fault_id": p.fault_id, "kind": "partial_window", "mode": p.mode, "target": target,
                        "expected": {"detectable": True, "raises": "ContinuityError", "gap_paise": p.amount_paise,
                                     "break_on": p.break_on.isoformat(), "recovered_by": None,
                                     "recovered_known_at": None,
                                     **({"state_unchanged": True} if p.mode == "batch_fails" else
                                        {"accept_gap": {"from_date": p.break_on.isoformat(),
                                                        "to_date": p.break_on.isoformat(), "gap_paise": p.amount_paise},
                                         "rows_with_lowered_dq": 0})}})
            continue
        x = {"detectable": True, "raises": "ContinuityError", "gap_paise": p.amount_paise,
             "break_on": p.break_on.isoformat(), "recovered_by": later.payload_id,
             "recovered_known_at": later.fetched_at.isoformat()}
        if p.mode == "batch_fails":
            x["state_unchanged"] = True
        else:
            x["accept_gap"] = {"from_date": p.break_on.isoformat(), "to_date": p.break_on.isoformat(),
                               "gap_paise": p.amount_paise}
            capped = expected_tx.filter((pl.col("dq_score") < 1) & (pl.col("known_at") == pd.fetched_at))
            x["rows_with_lowered_dq"] = capped.height
        log.append({"fault_id": p.fault_id, "kind": "partial_window", "mode": p.mode, "target": target,
                    "expected": x})
    return log


def _rates(pairs: list[str]) -> dict[str, float]:
    out = {}
    for pr in pairs:
        name, _, value = pr.partition("=")
        if name not in faults.FAULTS:
            raise SystemExit(f"unknown fault {name!r}; one of {faults.FAULTS}")
        out[f"{name}_rate"] = float(value)
    return out


def _configs(a) -> tuple[windows.WindowConfig, faults.FaultConfig]:
    return (windows.WindowConfig(cadence_days=a.cadence_days, overlap_days=a.overlap_days),
            faults.FaultConfig(seed=a.seed, partial_mode=a.partial_mode, **_rates(a.rate)))


def _print_score(r: dict) -> None:
    t, b = r["tables"]["ledger.transactions"], r["tables"]["ledger.balances"]
    for w in r["warnings"]:
        print(f"WARNING: {w}")
    print(f"PASS: {r['pass']}   deliveries: {r['deliveries']}   verdict: {r['stress']['verdict']}")
    for k, v in r["checks"].items():
        print(f"  {'ok  ' if v else 'FAIL'} {k}")
    print(f"ledger.transactions  expected {t['expected']}  actual {t['actual']}  exact {t['exact']}  "
          f"missing {t['missing']}  extra {t['extra']}  field mismatches {t['field_mismatch'] or 'none'}")
    ru = t["rupees"]
    print(f"  rupees reconciled {ru['reconciled_abs_paise'] / 100:,.2f} of {ru['expected_abs_paise'] / 100:,.2f} "
          f"(net expected {ru['expected_net_paise'] / 100:,.2f}, actual {ru['actual_net_paise'] / 100:,.2f})")
    print(f"ledger.balances  bank_reported {b['bank_reported']}  reconstructed {b['reconstructed']}")
    if r.get("matching"):
        _print_matching(r["matching"])
    if "ledger.invoices" in r["tables"]:
        i = r["tables"]["ledger.invoices"]
        print(f"ledger.invoices  expected {i['expected']}  actual {i['actual']}  exact {i['exact']}  missing {i['missing']}  "
              f"extra {i['extra']}  field mismatches {i['field_mismatch'] or 'none'}  rejects {i['rejects'] or 'none'}")
        print(f"  gross reconciled {i['gross_reconciled_paise'] / 100:,.2f} of {i['gross_expected_paise'] / 100:,.2f}  "
              f"buyer resolution {i['resolution_methods']}")
        for fmt, x in i["by_format"].items():
            print(f"  {fmt:14s} rendered {x['invoices_rendered']}  expected {x['expected']}  exact {x['exact']}  "
                  f"rejects {x['rejects'] or 'none'}  resolution {x['resolution']}  terms {x['terms_source']}  "
                  f"irn {x['with_irn']}")
    print(f"ledger.coverage  actual {r['tables']['ledger.coverage']['actual']}  "
          f"expected {r['tables']['ledger.coverage']['expected']}")
    print(f"stress  {r['stress']}")
    failed = [f for f in r["faults"] if not f["pass"]]
    print(f"faults: {len(r['faults']) - len(failed)}/{len(r['faults'])} behaved as expected")
    for f in failed[:20]:
        print(f"  FAIL {f['fault_id']}  expected {f['expected']}  observed {f['observed']}")
    print("named mismatches:")
    for k, v in r["named_mismatches"].items():
        print(f"  {k}: {v}")


def _print_matching(mt: dict) -> None:
    rows = [("link precision", "precision"), ("link recall", "recall"),
            ("rupees correctly allocated / credited", "rupees_allocated_share"),
            ("invoices paid in truth (denominator)", "paid_truth"),
            ("paid-date error, all truth-paid, mean", "paid_err_full_mean"),
            ("paid-date error, all truth-paid, p50", "paid_err_full_p50"),
            ("paid-date error, all truth-paid, p95", "paid_err_full_p95"),
            ("paid-date error, all truth-paid, max", "paid_err_full_max"),
            ("paid-date error, dated only, mean", "paid_err_dated_mean"),
            ("paid-date error, dated only, p95", "paid_err_dated_p95"),
            ("truth-paid, no date: open label", "paid_no_date_open_label"),
            ("truth-paid, no date: no label (review)", "paid_no_date_no_label"),
            ("paid when truth is open (false paid)", "false_paid"),
            ("TReDS discounts labelled treds_discount", "treds_labelled"), ("TDS rate accuracy", "tds_rate_accuracy"),
            ("TDS basis accuracy", "tds_basis_accuracy"), ("payer_kind accuracy", "payer_kind_accuracy"),
            ("review set / credits considered", "review_share"), ("unexplained credit rupees", "unexplained_rupees")]
    print("matching vs truth (n in brackets)            A6 cascade            exact-gross baseline")
    for label, k in rows:
        a, b = mt["a6"].get(k), mt["baseline"].get(k)
        fmt = lambda x: "-" if x is None else (f"{x['value']:,.3f} [{x['n']}]" if isinstance(x["value"], float)  # noqa: E731
                                                else f"{x['value']:,} [{x['n']}]")
        print(f"  {label:42s} {fmt(a):22s} {fmt(b)}")
    print(f"  stages {mt['a6_stages']}   passes {mt['a6_stats']['passes']}   review items {mt['a6_stats']['review_items']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a contract world to AA payloads and score A4 on them.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("render", "loop"):
        p = sub.add_parser(name)
        p.add_argument("world_dir")
        p.add_argument("out_dir")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--cadence-days", type=int, default=7)
        p.add_argument("--overlap-days", type=int, default=3)
        p.add_argument("--partial-mode", choices=faults.PARTIAL_MODES, default="batch_fails")
        p.add_argument("--rate", action="append", default=[], metavar="FAULT=SHARE")
        p.add_argument("--closed-loop", action="store_true", help="A2 re-fetches from coverage after any failure")
        p.add_argument("--invoice-mix", default=None, metavar="FORMAT=SHARE,...",
                       help=f"invoice formats and shares (default {DEFAULT_INVOICE_MIX})")
    sp = sub.add_parser("score")
    sp.add_argument("out_dir")
    sw = sub.add_parser("sweep")
    sw.add_argument("world_dir")
    sw.add_argument("--levels", default="0,5,10,25,50,75,100", help="percent")
    sw.add_argument("--seeds", default="0,1,2")
    sw.add_argument("--faults", default="all")
    sw.add_argument("--modes", default="batch_fails,accept_gap", help="partial_mode values to sweep")
    sw.add_argument("--loops", default="open,closed", help="open: A2 never reacts; closed: A2 re-fetches")
    sw.add_argument("--no-combos", action="store_true", help="skip the combined and no_anchors runs")
    sw.add_argument("--matchers", default="default", help="matcher variants (sweep.MATCHERS), comma-separated")
    sw.add_argument("--fault-extra", action="append", default=[], metavar="FIELD=VALUE",
                    help="extra FaultConfig field, e.g. coincidental_basis=taxable_value")
    sw.add_argument("--out-dir", default=str(Path(__file__).resolve().parents[1] / "outputs"))
    a = ap.parse_args()
    if a.cmd == "sweep":
        from render.sweep import main_sweep
        main_sweep(a)
        return
    if a.cmd in ("render", "loop"):
        mix = {k: float(v) for k, v in (kv.split("=") for kv in a.invoice_mix.split(","))} if a.invoice_mix else None
        out = render(a.world_dir, a.out_dir, *_configs(a), closed_loop=a.closed_loop, invoice_mix=mix)
        print(f"rendered {out}")
    if a.cmd in ("score", "loop"):
        from render.score import score
        _print_score(score(a.out_dir))


if __name__ == "__main__":
    main()
