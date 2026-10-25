import filecmp
import json
import random
from datetime import date, datetime, time, timedelta, timezone

import polars as pl
import pytest

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.validate import frame
from render import faults, windows
from render.cli import render
from render.score import score
from render.world import WorldError
from matsya_platform.ingest.invoices import fiscal_year

TENANT = "7d3c2b1a-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ACCOUNT = "acc_tiny"
START = date(2025, 4, 1)
CUT = datetime(2025, 5, 31, 18, 0, tzinfo=timezone.utc)
UTC = timezone.utc
SUPPLIER = "29AAFCS1234K1Z2"
BUYERS = [("ABCCA1234F", "ACME TEXTILES LTD"), ("BBCCB5678G", "BETA FOODS PVT LTD"), ("CDCCC9012H", "GAMMA STEEL LTD")]


def make_world(folder, days: int = 61, seed: int = 1):
    """A small contract world: 3 rows a day, reported 0-2 days late, some after 18:30 UTC."""
    r = random.Random(seed)
    rows, n = [], 0
    for day in range(days):
        vd = START + timedelta(days=day)
        for seq in range(3):
            amount = r.choice([1, -1]) * r.randrange(100, 50_000) * 100
            gst_row = seq == 0 and vd.day == 20  # one GST payment a month names the supplier GSTIN
            if gst_row:
                amount = -abs(amount)
            known = datetime.combine(vd, time(), tzinfo=UTC) + timedelta(days=r.randrange(3),
                                                                          seconds=r.randrange(6 * 3600, 24 * 3600 - 1))
            bank_id = f"SYN{n:07d}"
            rows.append({"tenant_id": TENANT, "txn_uid": ids.txn_uid(TENANT, ACCOUNT, ids.txn_key_bank_id(bank_id, vd, amount)),
                         "account_id": ACCOUNT, "value_date": vd, "known_at": known, "amount_paise": amount,
                         "direction": "credit" if amount > 0 else "debit",
                         "narration_raw": f"GST/CPIN/{n:014d}/{SUPPLIER}/TAX PAYMENT" if gst_row
                         else f"UPI/{'CR' if amount > 0 else 'DR'}/{n}",
                         "source": "synth", "dq_score": 1.0, "dup_group_id": None,
                         "category": "gst_payment" if gst_row else "misc_income", "rail": "UPI",
                         "process": "unscheduled", "bank_txn_id": bank_id, "day_seq": seq})
            n += 1
    truth = pl.DataFrame(rows)
    contract_cols = list(T.Transaction.model_fields)
    known = truth.filter(pl.col("known_at") <= CUT).select(contract_cols)
    tx = frame(T.Transaction, [T.Transaction(**x) for x in known.iter_rows(named=True)])
    (folder / "truth").mkdir(parents=True)
    tx.write_parquet(folder / "ledger.transactions.parquet")
    truth.write_parquet(folder / "truth" / "transactions.parquet")
    pl.DataFrame({"tenant_id": [TENANT], "account_id": [ACCOUNT], "date": [START - timedelta(days=1)],
                  "closing_paise": [5_00_00_000], "continuity_ok": [True]}).write_parquet(folder / "truth" / "balances.parquet")
    _write_invoices(folder, r)
    return folder


def _write_invoices(folder, r):
    buyers = [T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=f"27{pan}1Z5", name=name, type="listed",
                      sector="textiles", sector_source="exchange", treds_mandated=None) for pan, name in BUYERS]
    frame(T.Buyer, buyers).write_parquet(folder / "ref.buyers.parquet")
    frame(T.TenantBuyer, [T.TenantBuyer(tenant_id=TENANT, buyer_id=b.buyer_id, display_name=None, owner_sector=None,
                                        written_agreement=None, treds_onboarded=None) for b in buyers]
          ).write_parquet(folder / "ref.tenant_buyers.parquet")
    invs, truth = [], []
    for k in range(24):
        issue = START + timedelta(days=2 * k)
        no = f"INV-{k:05d}"
        unknown = k % 5 == 0
        gross = r.randrange(1_000, 3_00_000) * 100 + r.randrange(100)
        inv_id = ids.invoice_id(TENANT, SUPPLIER, no, fiscal_year(issue))
        invs.append(T.Invoice(tenant_id=TENANT, invoice_id=inv_id, irn=None, buyer_id=buyers[k % 3].buyer_id,
                              issue_date=issue, due_date=issue + timedelta(days=45 if unknown else 30),
                              terms_days=None if unknown else 30, terms_source="unknown" if unknown else "document_terms",
                              gross_paise=gross, gst_paise=round(gross * 18 / 118), expected_tds_paise=gross // 100,
                              status="open", known_at=datetime.combine(issue, time(), tzinfo=UTC)))
        truth.append({"invoice_id": inv_id, "invoice_no": no, "issue_date": issue})
    frame(T.Invoice, invs).write_parquet(folder / "ledger.invoices.parquet")
    pl.DataFrame(truth).write_parquet(folder / "truth" / "invoices.parquet")


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path / "world")


BASE_RATES = {"duplicate_payload": 0.3, "drop_txn_id": 0.05, "drop_all_ids": 0.0, "drop_balances": 0.0,
              "partial_window": 0.5, "duplicate_row": 0.15, "missing_gstin": 0.15, "drop_summary": 0.0,
              "identical_rows": 0.0, "split_day_windows": 0.0, "coincidental_credit": 0.0}


def cfgs(**kw):
    """Fault rates for the tiny world; fault=False turns one off, fault=<share> sets it."""
    rates = dict(BASE_RATES)
    for k in list(kw):
        if k in rates:
            v = kw.pop(k)
            rates[k] = 0.0 if v is False else v
    return (windows.WindowConfig(cadence_days=7, overlap_days=3),
            faults.FaultConfig(**{f"{k}_rate": v for k, v in rates.items()}, **kw))


@pytest.mark.parametrize("mode", faults.PARTIAL_MODES)
def test_loop_passes_with_every_fault(world, tmp_path, mode):
    out = render(world, tmp_path / "out", *cfgs(partial_mode=mode))
    report = score(out)
    assert report["pass"], json.dumps(report["checks"])
    kinds = {f["kind"] for f in report["faults"]}
    assert kinds == {"duplicate_payload", "drop_txn_id", "partial_window", "duplicate_row", "missing_gstin"}


def test_render_is_byte_identical(world, tmp_path):
    a = render(world, tmp_path / "a", *cfgs())
    b = render(world, tmp_path / "b", *cfgs())
    cmp = filecmp.dircmp(a, b)
    assert not cmp.diff_files and not cmp.left_only and not cmp.right_only
    assert not filecmp.dircmp(a / "payloads", b / "payloads").diff_files
    assert not filecmp.dircmp(a / "expected", b / "expected").diff_files


def _by_kind(out):
    log = json.loads((out / "fault_log.json").read_text())["faults"]
    return {k: [f for f in log if f["kind"] == k] for k in ("drop_txn_id", "partial_window", "duplicate_payload")}


def test_faults_toggle_independently(world, tmp_path):
    all_on = _by_kind(render(world, tmp_path / "on", *cfgs()))
    no_dup = _by_kind(render(world, tmp_path / "off", *cfgs(duplicate_payload=False)))
    assert no_dup["duplicate_payload"] == []
    assert [f["target"]["truth_txn_uid"] for f in no_dup["drop_txn_id"]] == \
        [f["target"]["truth_txn_uid"] for f in all_on["drop_txn_id"]]
    assert no_dup["partial_window"][0]["target"] == all_on["partial_window"][0]["target"]


def test_payload_format(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_payload=False, drop_txn_id=False, partial_window=False))
    manifest = json.loads((out / "manifest.json").read_text())
    for d in manifest["deliveries"]:
        acc = json.loads((out / d["file"]).read_text())["Account"]
        block = acc["Transactions"]
        assert (block["startDate"], block["endDate"]) == (d["start"], d["end"])
        for t in block["Transaction"]:
            assert t["type"] in ("CREDIT", "DEBIT") and not t["amount"].startswith("-")
            assert isinstance(t["currentBalance"], str) and "txnId" in t
        if block["Transaction"]:
            assert acc["Summary"]["currentBalance"] == block["Transaction"][-1]["currentBalance"]
        assert acc["Summary"]["balanceDateTime"].startswith(d["end"])


def test_rows_are_bucketed_by_ist_date(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_payload=False, drop_txn_id=False, partial_window=False))
    manifest = json.loads((out / "manifest.json").read_text())
    rmap = pl.read_parquet(out / "row_map.parquet")
    truth = pl.read_parquet(world / "truth" / "transactions.parquet").select("txn_uid", "known_at")
    minute = pl.col("known_at").dt.hour().cast(pl.Int32) * 60 + pl.col("known_at").dt.minute().cast(pl.Int32)
    late_utc = truth.filter(minute >= 18 * 60 + 30)
    assert late_utc.height > 0
    windows_of = {d["payload_id"]: (date.fromisoformat(d["start"]), date.fromisoformat(d["end"]))
                  for d in manifest["deliveries"]}
    j = rmap.join(late_utc, left_on="truth_txn_uid", right_on="txn_uid")
    for pid, ka in zip(j["payload_id"], j["known_at"]):
        s, e = windows_of[pid]
        assert s <= ka.date() + timedelta(days=1) <= e  # the IST date, one day after the UTC date


def test_overlap_must_be_at_least_three_days():
    with pytest.raises(ValueError):
        windows.WindowConfig(overlap_days=2)


def test_invalid_world_fails_loudly(world, tmp_path):
    p = world / "ledger.transactions.parquet"
    pl.read_parquet(p).with_columns(pl.col("amount_paise").cast(pl.Float64)).write_parquet(p)
    with pytest.raises(WorldError, match="does not validate"):
        render(world, tmp_path / "out", *cfgs())


def test_scoring_catches_a_tampered_payload(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_payload=False, drop_txn_id=False, partial_window=False))
    d = json.loads((out / "manifest.json").read_text())["deliveries"][-1]
    doc = json.loads((out / d["file"]).read_text())
    t = doc["Account"]["Transactions"]["Transaction"][-1]
    t["narration"] = "TAMPERED"  # a fact the scorer must notice; amounts would trip continuity instead
    (out / d["file"]).write_text(json.dumps(doc))
    report = score(out)
    assert not report["pass"] and report["tables"]["ledger.transactions"]["field_mismatch"] == {"narration_raw": 1}


def test_invoice_round_trip(world, tmp_path):
    report = score(render(world, tmp_path / "out", *cfgs()))
    assert report["pass"], json.dumps(report["checks"])
    inv = report["tables"]["ledger.invoices"]
    kinds = [f["kind"] for f in report["faults"]]
    assert kinds.count("duplicate_row") >= 1 and kinds.count("missing_gstin") >= 1
    tally = inv["by_format"]["tally_daybook"]
    assert inv["exact"] == inv["expected"] == 24
    # Name resolution: every missing_gstin CSV row, and every day book voucher (no GSTIN shown).
    assert inv["resolution_methods"]["name"] == kinds.count("missing_gstin") + tally["actual"]
    assert set(inv["by_format"]) == {"csv_generic", "tally_daybook", "gst_einvoice"}
    for fmt, x in inv["by_format"].items():
        assert x["exact"] == x["expected"] == x["invoices_rendered"] > 0, fmt
    assert inv["by_format"]["gst_einvoice"]["with_irn"] == inv["by_format"]["gst_einvoice"]["actual"]


def test_invoice_csv_is_sloppy_and_omits_unknown_terms(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_row=False, missing_gstin=False),
                 invoice_mix={"csv_generic": 1.0})
    raw = (out / "invoices" / "invoices.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and raw.endswith(b",,,,,,,,\r\n")
    text = raw.decode("utf-8-sig").splitlines()
    unknown = [line for line in text[1:] if line.startswith("INV-00000,")]  # k = 0 has unknown terms
    assert unknown and unknown[0].split(",")[4:6] == ["", ""]
    assert any('"' in line and "," in line.split('"')[1] for line in text[1:])  # comma-grouped rupees


def test_invoice_faults_toggle_independently(world, tmp_path):
    on = _by_kind_inv(render(world, tmp_path / "on", *cfgs()))
    off = _by_kind_inv(render(world, tmp_path / "off", *cfgs(duplicate_row=False)))
    assert off["duplicate_row"] == [] and off["missing_gstin"] == on["missing_gstin"]


def _by_kind_inv(out):
    log = json.loads((out / "fault_log.json").read_text())["faults"]
    return {k: [f["target"]["invoice_id"] for f in log if f["kind"] == k] for k in ("duplicate_row", "missing_gstin")}


def test_accept_gap_comes_from_the_fault_log(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(partial_mode="accept_gap"))
    p = out / "fault_log.json"
    log = json.loads(p.read_text())
    for f in log["faults"]:
        if f["kind"] == "partial_window":
            f["expected"]["accept_gap"]["gap_paise"] += 1  # the operator accepts a different gap than A4 sees
    p.write_text(json.dumps(log))
    report = score(out)
    partials = [f for f in report["faults"] if f["kind"] == "partial_window"]
    assert partials and not report["pass"]
    assert all(not f["pass"] and f["observed"]["raised_after_accept"] for f in partials)


def test_report_warns_that_the_cut_is_inferred(world, tmp_path):
    report = score(render(world, tmp_path / "out", *cfgs()))
    assert any("cut inferred" in w for w in report["warnings"])


@pytest.mark.parametrize("fault", ["drop_all_ids", "drop_balances"])
def test_id_faults_change_the_key_path(world, tmp_path, fault):
    report = score(render(world, tmp_path / "out", *cfgs(drop_txn_id=False, **{fault: 1.0})))
    assert report["pass"], json.dumps(report["checks"])
    paths = report["stress"]["key_paths"]
    if fault == "drop_all_ids":
        assert paths["ord"] == report["stress"]["ledger_rows"]
    else:
        assert paths["txn"] == report["stress"]["ledger_rows"]  # txnId still there; only anchors are gone


def test_stress_flags_a_silent_error(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_payload=False, drop_txn_id=False, partial_window=False))
    d = json.loads((out / "manifest.json").read_text())["deliveries"][-1]
    doc = json.loads((out / d["file"]).read_text())
    doc["Account"]["Transactions"]["Transaction"][-1]["narration"] = "TAMPERED"
    (out / d["file"]).write_text(json.dumps(doc))
    s = score(out)["stress"]
    assert s["verdict"] == "SILENT" and s["wrong_facts"] == 1


def test_identical_rows_with_ids_stay_exact(world, tmp_path):
    report = score(render(world, tmp_path / "out", *cfgs(identical_rows=1.0)))
    assert report["stress"]["verdict"] == "exact"
    assert report["stress"]["ledger_rows"] == report["stress"]["truth_rows"] == 2 * 164


def test_split_windows_without_anchors_lose_identical_rows_loudly(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(identical_rows=1.0, split_day_windows=1.0, drop_all_ids=1.0,
                                                drop_balances=1.0, drop_summary=1.0, partial_window=False))
    report = score(out)
    s = report["stress"]
    assert report["named_mismatches"]["identity_collisions"] > 0
    # The collided rows are lost, but every payload that carried them is flagged unverified.
    assert s["verdict"] == "loud" and s["silent_total"] == 0 and s["unverified_payloads"] > 0
    assert s["missing_rows_loud"] >= report["named_mismatches"]["identity_collisions"]


def test_undetectable_partial_window_is_flagged_unverified(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(partial_window=1.0, drop_balances=1.0, drop_summary=1.0,
                                                drop_txn_id=False, duplicate_payload=False))
    report = score(out)
    s = report["stress"]
    assert report["named_mismatches"]["undetectable_partials"] > 0 and s["continuity_failures"] == 0
    assert s["verdict"] == "loud" and s["silent_total"] == 0 and s["missing_rows_loud"] > 0


def test_closed_loop_recovers_where_open_loop_freezes(world, tmp_path):
    kw = dict(identical_rows=0.5, drop_txn_id=0.5, partial_window=False, duplicate_payload=False)
    wcfg, fcfg = cfgs(**kw)
    opened = score(render(world, tmp_path / "open", wcfg, fcfg))["stress"]
    closed = score(render(world, tmp_path / "closed", wcfg, fcfg, closed_loop=True))["stress"]
    assert closed["transactions_exact_share"] >= opened["transactions_exact_share"]
    assert closed["silent_total"] == 0


def test_coincidental_credit_matches_an_invoice_net_exactly(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(coincidental_credit=1.0, partial_window=False, duplicate_payload=False,
                                                drop_txn_id=False))
    inj = pl.read_parquet(out / "truth" / "coincidental.parquet")
    inv = pl.read_parquet(world / "ledger.invoices.parquet")
    assert inj.height > 0
    j = inj.join(inv, on="invoice_id")
    assert (j["amount_paise"] == j["gross_paise"]).all()  # no truth buyers in the tiny world: 0% TDS
    rmap = pl.read_parquet(out / "row_map.parquet")
    # Delivered like any other credit; ones posted after the last fetch are not, as for any row.
    assert set(inj["truth_txn_uid"]) & set(rmap["truth_txn_uid"])
    again = render(world, tmp_path / "again", *cfgs(coincidental_credit=1.0, partial_window=False,
                                                    duplicate_payload=False, drop_txn_id=False))
    assert pl.read_parquet(again / "truth" / "coincidental.parquet").equals(inj)


def test_day_book_and_e_invoices_omit_unknown_terms(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs(duplicate_row=False, missing_gstin=False),
                 invoice_mix={"tally_daybook": 0.5, "gst_einvoice": 0.5})
    rmap = pl.read_parquet(out / "invoices" / "row_map.parquet")
    first = pl.read_parquet(world / "ledger.invoices.parquet")
    unknown = set(first.filter(pl.col("terms_source") == "unknown")["invoice_id"])
    docs = json.loads((out / "invoices" / "einvoices.json").read_text())
    for row in rmap.filter(pl.col("format") == "gst_einvoice").iter_rows(named=True):
        pay = docs[row["source_row"] - 1]["PayDtls"]
        assert ("CrDay" in pay) == (row["invoice_id"] not in unknown)
    lines = (out / "invoices" / "daybook.csv").read_text().splitlines()
    bills = [ln for ln in lines if ln.startswith(",New Ref ")]
    assert bills and any(" Due on " not in b and " Days" not in b for b in bills)  # unknown terms: neither
    assert any(",Receipt," in ln or ",Payment," in ln for ln in lines)  # noise vouchers the reader skips
    assert not any("27" + pan in "".join(lines) for pan, _ in BUYERS)  # a day book shows no GSTIN


def test_e_invoices_only_for_buyers_with_a_gstin(world, tmp_path):
    b = pl.read_parquet(world / "ref.buyers.parquet")
    b.with_columns(pl.lit(None, pl.String).alias("gstin")).write_parquet(world / "ref.buyers.parquet")
    out = render(world, tmp_path / "out", *cfgs(duplicate_row=False, missing_gstin=False),
                 invoice_mix={"gst_einvoice": 1.0})
    assert not (out / "invoices" / "einvoices.json").exists()
    assert set(pl.read_parquet(out / "invoices" / "row_map.parquet")["format"]) == {"csv_generic"}


def test_loop_fetches_under_an_active_consent_into_the_encrypted_raw_store(world, tmp_path):
    out = render(world, tmp_path / "out", *cfgs())
    report = score(out)
    assert report["pass"], json.dumps(report["checks"])
    from render.score import LOOP_CONSENT, services

    svc = services(out / "store")
    tenant = json.loads((out / "manifest.json").read_text())["deliveries"][0]["tenant_id"]
    assert svc.get(tenant, LOOP_CONSENT).status == "active"
    stored = svc.tables.read("raw.aa_payloads", tenant)
    deliveries = json.loads((out / "manifest.json").read_text())["deliveries"]
    originals = [d for d in deliveries if not d["duplicate_of"]]
    assert stored.height == len(originals) and set(stored["consent_id"]) == {LOOP_CONSENT}
    for d in originals:  # the ledger was built from these bytes, decrypted from the raw store
        assert svc.raw.get(tenant, d["payload_id"]) == (out / d["file"]).read_bytes()
        assert not svc.raw.path(tenant, d["payload_id"]).read_bytes().startswith(b"{")


def test_repeat_delivery_is_dropped_at_fetch(world, tmp_path):
    report = score(render(world, tmp_path / "out", *cfgs(duplicate_payload=1.0, partial_window=False)))
    dups = [f for f in report["faults"] if f["kind"] == "duplicate_payload"]
    assert dups and all(f["pass"] and f["observed"]["deduplicated"] and f["observed"]["new_bank_reported"] == 0
                        for f in dups)
    assert report["stress"]["verdict"] == "exact"


def test_revoke_mid_run_discards_the_in_flight_payload_and_erases(world, tmp_path):
    from render.score import LOOP_CONSENT, open_consent, run_a4, services

    out = render(world, tmp_path / "out", *cfgs(duplicate_payload=False, partial_window=False))
    manifest = json.loads((out / "manifest.json").read_text())
    meta = json.loads((out / "expected" / "meta.json").read_text())
    svc = services(out / "store")
    open_consent(svc, manifest, datetime.fromisoformat(meta["cut"]))
    k = 3
    _, log = run_a4(out, manifest, json.loads((out / "fault_log.json").read_text())["faults"], svc, revoke_at=k)
    assert all("new_txns" in e for e in log[:k])  # built before the revoke
    assert log[k].get("discarded") is True  # the payload in hand when the owner revoked
    assert all(e.get("not_fetched") for e in log[k + 1:])  # nothing fetched after
    tenant = manifest["deliveries"][0]["tenant_id"]
    assert svc.get(tenant, LOOP_CONSENT).status == "revoked"
    assert svc.raw.objects() == [] and manifest["deliveries"][k]["payload_id"] not in {
        pid for _, pid in svc.raw.objects()}
    assert svc.tables.read("ledger.transactions", tenant).is_empty()  # erased with the key
    assert tenant not in svc.keys.tenants()
