"""The product pipeline (matsya_platform.pipeline) on rendered worlds: incremental runs agree
with one run over everything, a repeat run writes nothing, and the consent, lock and
continuity rules hold across runs."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from matsya_contracts.enums import ConsentStatus as S
from matsya_platform.pipeline import config as config_mod
from matsya_platform.pipeline.__main__ import main
from matsya_platform.pipeline.run import RunInProgress, grant, lock, run, services, set_status
from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame
from render.cli import render
from test_render import ACCOUNT, BUYERS, CUT, TENANT, cfgs, make_world

UTC = timezone.utc


def world_with_payments(folder: Path) -> Path:
    """The tiny world plus buyer payments for every other invoice, 20 days after issue: exact
    gross, or gross less 1% TDS on the taxable value, half of them naming the invoice."""
    make_world(folder)
    truth = pl.read_parquet(folder / "truth" / "transactions.parquet")
    invoices = pl.read_parquet(folder / "ledger.invoices.parquet").join(
        pl.read_parquet(folder / "truth" / "invoices.parquet").select("invoice_id", "invoice_no"), on="invoice_id")
    names = {ids.buyer_id(pan): name for pan, name in BUYERS}
    end = truth["value_date"].max()
    rows = []
    for k, inv in enumerate(invoices.sort("issue_date").iter_rows(named=True)):
        vd = inv["issue_date"] + timedelta(days=20)
        if k % 2 or vd > end:
            continue
        amount = inv["gross_paise"] - (round((inv["gross_paise"] - inv["gst_paise"]) / 100) if k % 4 == 2 else 0)
        ref = f"/{inv['invoice_no']}" if k % 4 == 0 else ""
        bank_id = f"PAY{k:05d}"
        rows.append({**truth.row(0, named=True), "txn_uid": ids.txn_uid(TENANT, ACCOUNT, ids.txn_key_bank_id(bank_id, vd, amount)),
                     "value_date": vd, "known_at": datetime.combine(vd, datetime.min.time(), tzinfo=UTC) + timedelta(days=1, hours=10),
                     "amount_paise": amount, "direction": "credit",
                     "narration_raw": f"BY NEFT/UTR{k:08d}/{names[inv['buyer_id']]}{ref}", "category": "customer_receipt",
                     "rail": "NEFT", "process": "scheduled", "bank_txn_id": bank_id, "day_seq": 3})
    truth = pl.concat([truth, pl.DataFrame(rows, schema=truth.schema)]).sort("value_date", "day_seq")
    truth.write_parquet(folder / "truth" / "transactions.parquet")
    known = truth.filter(pl.col("known_at") <= CUT).select(list(T.Transaction.model_fields))
    frame(T.Transaction, [T.Transaction(**x) for x in known.iter_rows(named=True)]).write_parquet(
        folder / "ledger.transactions.parquet")
    return folder


def setup(world, tmp: Path, name: str, **faults) -> tuple[config_mod.PipelineConfig, dict, Path]:
    out = render(world, tmp / f"{name}-rendered", *cfgs(**faults))
    manifest = json.loads((out / "manifest.json").read_text())
    inbox = tmp / f"{name}-inbox"
    for f in manifest["invoices"]["files"]:
        (inbox / f["adapter"]).mkdir(parents=True, exist_ok=True)
        shutil.copy(out / f["file"], inbox / f["adapter"])
    m = manifest["invoices"]
    (tmp / f"{name}.toml").write_text(f'''store = "{name}-store"
export_dir = "{name}-contract"
ref_dir = "{out / 'ref'}"

[[tenant]]
tenant_id = "{m['tenant_id']}"
supplier_gstin = "{m['supplier_gstin']}"
aa_folder = "{out}"
invoice_inbox = "{inbox}"
[tenant.consent]
valid_days = 1000
''')
    return config_mod.load(tmp / f"{name}.toml"), manifest, out


def fetch_times(manifest) -> list[datetime]:
    return sorted({datetime.fromisoformat(d["fetched_at"]) for d in manifest["deliveries"]})


def stored(cfg, tenant):
    t = services(cfg.store).tables
    latest = lambda df, key: df.sort("known_at").group_by(key, maintain_order=True).last().drop("known_at")
    return {"txns": t.read("ledger.transactions", tenant).sort("txn_uid"),
            "balances": latest(t.read("ledger.balances", tenant), ["account_id", "date", "balance_source"])
            .sort("account_id", "date", "balance_source"),
            # known_at is when the pipeline received the file, so it differs between schedules.
            "invoices": t.read("ledger.invoices", tenant).drop("known_at").sort("invoice_id"),
            "matches": latest(t.read("ledger.matches", tenant), ["txn_uid", "invoice_id"]).sort("txn_uid", "invoice_id"),
            "labels": latest(t.read("labels.invoice_outcomes", tenant), ["invoice_id"]).sort("invoice_id")}


def test_weekly_runs_end_where_one_run_over_everything_ends(tmp_path):
    world = world_with_payments(tmp_path / "world")
    weekly, manifest, _ = setup(world, tmp_path, "weekly", partial_window=False)
    once, _, _ = setup(world, tmp_path, "once", partial_window=False)
    tenant = weekly.tenants[0].tenant_id
    times = fetch_times(manifest)
    start, end = times[0] - timedelta(hours=1), times[-1] + timedelta(hours=1)
    for cfg in (weekly, once):
        grant(cfg, tenant, start)
    for t in times:
        assert run(weekly, t + timedelta(minutes=5)).ok
    final = run(weekly, end)
    one = run(once, end)
    assert final.ok and one.ok and one.tenants[0].built == len(manifest["deliveries"]) - sum(
        1 for d in manifest["deliveries"] if d["duplicate_of"])
    a, b = stored(weekly, tenant), stored(once, tenant)
    for k in a:
        assert a[k].height > 0 and a[k].equals(b[k]), k
    assert (weekly.export_dir / "ledger.matches.parquet").exists()


def test_a_repeat_run_writes_nothing(tmp_path):
    cfg, manifest, _ = setup(make_world(tmp_path / "world"), tmp_path, "p", partial_window=False)
    tenant = cfg.tenants[0].tenant_id
    times = fetch_times(manifest)
    grant(cfg, tenant, times[0] - timedelta(hours=1))
    end = times[-1] + timedelta(hours=1)
    run(cfg, end)
    objects = services(cfg.store).tables.backend
    before = objects.list_keys("")
    again = run(cfg, end)
    assert objects.list_keys("") == before
    t = again.tenants[0]
    assert (t.fetched, t.built, t.invoice_files, t.matched["new_matches"], t.matched["new_labels"]) == (0, 0, 0, 0, 0)
    assert t.duplicates > 0 and t.replayed > 0


def test_a_broken_payload_is_logged_once_and_never_applied(tmp_path):
    cfg, manifest, _ = setup(make_world(tmp_path / "world"), tmp_path, "p", partial_window=1.0, partial_mode="batch_fails")
    tenant = cfg.tenants[0].tenant_id
    times = fetch_times(manifest)
    grant(cfg, tenant, times[0] - timedelta(hours=1))
    first = run(cfg, times[-1] + timedelta(hours=1)).tenants[0]
    expected = {d["payload_id"] for d in manifest["deliveries"] if d["expect"] == "fail"}
    assert expected and {f["payload_id"] for f in first.failed} == expected
    second = run(cfg, times[-1] + timedelta(hours=2)).tenants[0]
    assert second.failed == [] and second.built == 0 and second.replayed == first.built
    log = services(cfg.store).tables.read("aux.build_log", tenant)
    assert set(log.filter(pl.col("outcome") == "failed_continuity")["payload_id"]) == expected


def test_after_a_revoke_nothing_runs_and_nothing_comes_back(tmp_path):
    cfg, manifest, _ = setup(make_world(tmp_path / "world"), tmp_path, "p", partial_window=False)
    tenant = cfg.tenants[0].tenant_id
    times = fetch_times(manifest)
    grant(cfg, tenant, times[0] - timedelta(hours=1))
    run(cfg, times[-1] + timedelta(hours=1))
    assert set_status(cfg, tenant, "C1", S.REVOKED, times[-1] + timedelta(hours=2)) == "revoked"
    after = run(cfg, times[-1] + timedelta(hours=3)).tenants[0]
    assert after.consent is None and (after.fetched, after.invoice_files) == (0, 0)
    tables = services(cfg.store).tables
    for t in ("ledger.transactions", "ledger.invoices", "labels.invoice_outcomes", "raw.aa_payloads"):
        assert tables.read(t, tenant).is_empty(), t


def test_an_overlapping_run_is_refused(tmp_path):
    cfg, manifest, _ = setup(make_world(tmp_path / "world"), tmp_path, "p", partial_window=False)
    with lock(cfg.store):
        with pytest.raises(RunInProgress):
            run(cfg, fetch_times(manifest)[-1])
        assert main(["-c", str(tmp_path / "p.toml"), "run"]) == 2


def test_the_command_line_runs_a_replay(tmp_path, capsys):
    cfg, manifest, _ = setup(make_world(tmp_path / "world"), tmp_path, "p", partial_window=False)
    conf = str(tmp_path / "p.toml")
    times = fetch_times(manifest)
    tenant = cfg.tenants[0].tenant_id
    assert main(["-c", conf, "--now", (times[0] - timedelta(hours=1)).isoformat(), "consent", "grant", tenant]) == 0
    assert main(["-c", conf, "replay", "--start", (times[0] + timedelta(minutes=5)).isoformat(),
                 "--end", (times[-1] + timedelta(days=1)).isoformat(), "--step-days", "7"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "C1" and len(lines) > 2 and "ERROR" not in "".join(lines)
    assert main(["-c", conf, "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st[0]["payloads_applied"] > 0 and st[0]["consents"][0]["status"] == "active"
