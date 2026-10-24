import json
import subprocess
import sys
from datetime import date, datetime, timezone

import polars as pl
import pytest
from pydantic import ValidationError

from matsya_contracts import VERSION
from matsya_contracts import enums as E
from matsya_contracts import events as ev
from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.fixtures import generate
from matsya_contracts.validate import ContractError, fields, validate

UTC = timezone.utc
TENANT = "0f8c3a52-7d1e-4b6a-9c2f-3e5d7a9b1c4e"


@pytest.fixture(scope="module")
def fx() -> dict[str, pl.DataFrame]:
    return generate(0)


def test_version():
    assert VERSION == "0.1.0"


# ---------------------------------------------------------------- fixtures


def test_fixture_covers_every_table(fx):
    assert set(fx) == set(T.TABLES)
    assert all(df.height > 0 for df in fx.values())


@pytest.mark.parametrize("table", sorted(T.TABLES))
def test_fixture_table_validates(fx, table):
    validate(fx[table], table)


def test_fixtures_deterministic(fx):
    again = generate(0)
    assert all(fx[t].equals(again[t]) for t in fx)
    other = generate(1)
    assert any(not fx[t].equals(other[t]) for t in fx)


def test_fixtures_deterministic_across_processes(fx, tmp_path):
    code = "from matsya_contracts.fixtures import write; import sys; write(sys.argv[1], 0)"
    for name in ("a", "b"):
        subprocess.run([sys.executable, "-c", code, str(tmp_path / name)], check=True,
                       env={"PYTHONHASHSEED": "random", "PATH": ""})
    for table in fx:
        a = pl.read_parquet(tmp_path / "a" / f"{table}.parquet")
        b = pl.read_parquet(tmp_path / "b" / f"{table}.parquet")
        assert a.equals(b), table


ENUM_COLUMNS = [
    ("features.txn_category", "category", E.TxnCategory),
    ("features.txn_category", "source", E.CategorySource),
    ("ledger.transactions", "direction", E.Direction),
    ("ledger.transactions", "source", E.TxnSource),
    ("ledger.balances", "balance_source", E.BalanceSource),
    ("ledger.parsed", "rail", E.Rail),
    ("ledger.invoices", "status", E.InvoiceStatus),
    ("ledger.invoices", "terms_source", E.TermsSource),
    ("ledger.matches", "payer_kind", E.PayerKind),
    ("ledger.matches", "tds_basis", E.TdsBasis),
    ("ledger.matches", "method", E.MatchMethod),
    ("labels.invoice_outcomes", "settlement_kind", E.SettlementKind),
    ("ref.buyers", "type", E.BuyerType),
    ("ref.buyers", "id_basis", E.BuyerIdBasis),
    ("ref.buyers", "sector", E.Sector),
    ("ops.consents", "status", E.ConsentStatus),
    ("ops.dq_quarantine", "reason", E.QuarantineReason),
    ("ops.owner_inputs", "kind", E.OwnerInputKind),
    ("ops.events", "kind", E.EventKind),
    ("features.obligations", "kind", E.ObligationKind),
    ("features.obligations", "amount_owner", E.AmountOwner),
    ("features.sales_process", "name", E.SalesParam),
    ("decisions.plans", "action", E.ActionKind),
    ("decisions.drivers", "driver_kind", E.DriverKind),
]


@pytest.mark.parametrize("table,col,enum", ENUM_COLUMNS, ids=[f"{t}.{c}" for t, c, _ in ENUM_COLUMNS])
def test_fixture_has_every_enum_member(fx, table, col, enum):
    assert set(fx[table][col].drop_nulls()) == {m.value for m in enum}


def test_sector_source_coverage(fx):
    # owner is valid only via ref.tenant_buyers, so ref.buyers carries the other three.
    assert set(fx["ref.buyers"]["sector_source"]) == {"exchange", "cin_nic", "unknown"}
    assert fx["ref.tenant_buyers"]["owner_sector"].null_count() > 0


# ---------------------------------------------------------------- conventions


def test_money_is_int64_paise_everywhere():
    for table, model in T.TABLES.items():
        for col, (dtype, _, money) in fields(model).items():
            if col.endswith("_paise"):
                assert money, f"{table}.{col} ends in _paise but is not typed Paise"
            if money:
                assert dtype == pl.Int64(), f"{table}.{col} is money but {dtype}"
    for model in ev.PAYLOADS.values():
        for col, f in model.model_fields.items():
            if col.endswith("_paise"):
                assert f.annotation is int, f"{model.__name__}.{col} is not int"


def test_quantile_money_columns_are_paise():
    for table in ("forecast.balance_quantiles", "features.flow_quantiles"):
        assert all(fields(table)[q][2] for q in ("q05", "q25", "q50", "q75", "q95"))


def test_tenant_id_on_every_table_except_global_ref():
    assert T.GLOBAL_TABLES == {"ref.buyers", "ref.buyer_disclosures"}
    for table, model in T.TABLES.items():
        if table not in T.GLOBAL_TABLES:
            assert list(model.model_fields)[0] == "tenant_id", table


def test_one_writer_per_namespace():
    for table, model in T.TABLES.items():
        assert model.writer == T.NAMESPACE_WRITERS[table.split(".")[0]], table


def test_moved_namespaces():
    for gone in ("ref.obligations", "ref.buyer_priors", "ops.calibration_state", "ops.shock_state"):
        assert gone not in T.TABLES
    for new in ("features.obligations", "features.buyer_priors", "forecast.calibration_state",
                "forecast.shock_state"):
        assert new in T.TABLES


def test_as_of_tables_key_on_known_at():
    for table in ("ledger.balances", "ledger.invoices", "ledger.matches", "labels.invoice_outcomes"):
        assert "known_at" in T.TABLES[table].pk, table


def test_parsed_inherits_known_at():
    assert "known_at" not in T.Parsed.model_fields


def test_invoices_allow_versions(fx):
    assert fx["ledger.invoices"].group_by("tenant_id", "invoice_id").len()["len"].max() > 1


def test_category_enum_matches_track_b_list():
    assert [c.value for c in E.CATEGORIES] == [
        "customer_receipt", "supplier_payment", "salary", "rent", "emi", "professional_fee", "gst_payment",
        "tds_payment", "pf_esi", "bank_charges", "refund", "owner_transfer", "cash_withdrawal", "misc_income",
        "misc_expense"]
    assert E.TxnCategory.ABSTAIN not in E.CATEGORIES
    assert len(E.TxnCategory) == 16


def test_every_event_kind_has_a_payload():
    assert set(ev.PAYLOADS) == set(E.EventKind)


# ---------------------------------------------------------------- sign and direction


def _txn(**kw):
    base = dict(tenant_id=TENANT, txn_uid="a" * 32, account_id="ACC", value_date=date(2026, 5, 1),
                known_at=datetime(2026, 5, 2, tzinfo=UTC), amount_paise=100, direction="credit",
                narration_raw=None, source="aa", dq_score=1.0, dup_group_id=None)
    return T.Transaction(**{**base, **kw})


def test_sign_direction_agree():
    _txn(amount_paise=100, direction="credit")
    _txn(amount_paise=-100, direction="debit")


@pytest.mark.parametrize("amount,direction", [(100, "debit"), (-100, "credit"), (0, "credit"), (0, "debit")])
def test_sign_direction_mismatch_rejected(amount, direction):
    with pytest.raises(ValidationError):
        _txn(amount_paise=amount, direction=direction)


def test_validate_rejects_mismatch_frame_without_dropping(fx):
    df = fx["ledger.transactions"]
    bad = df.with_columns(pl.when(pl.int_range(pl.len()) == 0).then(pl.lit("debit") if df["amount_paise"][0] > 0
                                                                     else pl.lit("credit"))
                          .otherwise(pl.col("direction")).alias("direction"))
    with pytest.raises(ContractError, match="sign"):
        validate(bad, "ledger.transactions")
    assert validate(df, "ledger.transactions") is df


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        _txn(known_at=datetime(2026, 5, 2))


# ---------------------------------------------------------------- censor rule


def _outcome(**kw):
    base = dict(tenant_id=TENANT, invoice_id="b" * 32, buyer_id="c" * 32, issue_date=date(2026, 4, 1),
                first_paid_date=None, paid_date=None, paid_fraction=0.0, settlement_kind=None,
                censored_at=date(2026, 6, 1), known_at=datetime(2026, 6, 1, 23, tzinfo=UTC))
    return T.InvoiceOutcome(**{**base, **kw})


def test_open_invoice_is_censored():
    assert T.timing_state(_outcome()) == "censored"


def test_treds_discount_is_censored_despite_paid_date():
    o = _outcome(first_paid_date=date(2026, 5, 10), paid_date=date(2026, 5, 10), paid_fraction=1.0,
                 settlement_kind="treds_discount", censored_at=date(2026, 5, 10))
    assert o.paid_date is not None
    assert T.timing_state(o) == "censored"


def test_buyer_paid_is_paid():
    o = _outcome(first_paid_date=date(2026, 5, 1), paid_date=date(2026, 5, 10), paid_fraction=1.0,
                 settlement_kind="buyer", censored_at=None)
    assert T.timing_state(o) == "paid"


@pytest.mark.parametrize("kw", [
    dict(censored_at=None),  # open but not censored
    dict(first_paid_date=date(2026, 5, 10), paid_date=date(2026, 5, 10), paid_fraction=1.0,
         settlement_kind="treds_discount", censored_at=None),  # treds must censor
    dict(first_paid_date=date(2026, 5, 10), paid_date=date(2026, 5, 10), paid_fraction=1.0,
         settlement_kind="buyer"),  # paid by buyer must not censor
    dict(settlement_kind="buyer"),  # settlement_kind while open
    dict(first_paid_date=None, paid_date=date(2026, 5, 10), settlement_kind="buyer", censored_at=None),
    dict(paid_fraction=1.2),
    dict(censored_at=date(2026, 6, 2)),  # censored after known_at
], ids=["open-uncensored", "treds-uncensored", "buyer-censored", "kind-while-open", "no-first-paid",
        "fraction>1", "censor-after-known"])
def test_censor_rule_violations_rejected(kw):
    with pytest.raises(ValidationError):
        _outcome(**kw)


def test_fixture_outcomes_follow_censor_rule(fx):
    df = fx["labels.invoice_outcomes"]
    must = df["paid_date"].is_null() | (df["settlement_kind"] == "treds_discount").fill_null(False)
    assert (must == df["censored_at"].is_not_null()).all()
    assert df.filter(pl.col("paid_date").is_not_null() & pl.col("censored_at").is_not_null()).height > 0


# ---------------------------------------------------------------- other rules


def test_validate_rejects_wrong_columns_dtype_nulls_and_duplicate_keys(fx):
    df = fx["ledger.balances"]
    with pytest.raises(ContractError, match="columns"):
        validate(df.drop("known_at"), "ledger.balances")
    with pytest.raises(ContractError, match="dtype"):
        validate(df.with_columns(pl.col("closing_paise").cast(pl.Float64)), "ledger.balances")
    with pytest.raises(ContractError, match="nulls"):
        validate(df.with_columns(pl.lit(None, pl.String).alias("balance_source")), "ledger.balances")
    with pytest.raises(ContractError, match="duplicate key"):
        validate(pl.concat([df, df.head(1)]), "ledger.balances")


def test_validate_sample_mode(fx):
    df = fx["ledger.transactions"]
    bad = pl.concat([df, df.head(1).with_columns(
        pl.lit("f" * 32).alias("txn_uid"), (-pl.col("amount_paise")).alias("amount_paise"))])
    with pytest.raises(ContractError, match="sign"):
        validate(bad, "ledger.transactions")  # full check, as in CI
    validate(bad, "ledger.transactions", sample=0)  # columns, dtypes, nulls, keys only
    with pytest.raises(ContractError, match="nulls"):
        validate(df.with_columns(pl.lit(None, pl.Date).alias("value_date")), "ledger.transactions", sample=0)
    assert validate(df, "ledger.transactions", sample=5, seed=1) is df


def test_balances_allow_versions_of_one_date(fx):
    df = fx["ledger.balances"]
    assert df.group_by("tenant_id", "account_id", "date").len()["len"].max() > 1


def test_txn_key_paths_do_not_collapse_real_transactions():
    d = date(2026, 5, 1)
    uid = lambda k: ids.txn_uid(TENANT, "ACC", k)  # noqa: E731
    # A reused bank txnId on another date or amount is a different transaction.
    assert uid(ids.txn_key_bank_id("T1", d, 500)) != uid(ids.txn_key_bank_id("T1", date(2026, 6, 1), 500))
    assert uid(ids.txn_key_bank_id("T1", d, 500)) != uid(ids.txn_key_bank_id("T1", d, -500))
    # One UTR covering a payment and its bank charge.
    assert uid(ids.txn_key_utr("U1", 100_000)) != uid(ids.txn_key_utr("U1", -1_180))
    # The same running balance on two dates.
    assert uid(ids.txn_key_balance(5_000, d, 100)) != uid(ids.txn_key_balance(5_000, date(2026, 5, 9), 100))
    # Two identical same-day credits stay separate by ordinal.
    assert uid(ids.txn_key_ordinal(d, 100, "UPI CR", 0)) != uid(ids.txn_key_ordinal(d, 100, "UPI CR", 1))
    # Paths are tagged, so equal raw values on different paths never collide.
    assert ids.txn_key_bank_id("X", d, 1).startswith("txn:") and ids.txn_key_utr("X", 1).startswith("utr:")
    assert ids.txn_key_bank_id("T1", d, -500) == "txn:T1|2026-05-01|-500"
    assert ids.txn_key_utr("U1", -1_180) == "utr:U1|-1180"


def test_local_buyer_id():
    a = ids.local_buyer_id(TENANT, "  sharma   traders ")
    assert a == ids.local_buyer_id(TENANT, "SHARMA TRADERS")
    assert a == ids.hash32("noPAN:" + TENANT, "SHARMA TRADERS")
    assert a != ids.local_buyer_id("1" + TENANT[1:], "SHARMA TRADERS")


def test_id_formats():
    assert ids.txn_uid(TENANT, "ACC", "TXN1") == ids.txn_uid(TENANT, "ACC", "TXN1")
    assert len(ids.txn_uid(TENANT, "ACC", "TXN1")) == 32
    assert ids.invoice_id(TENANT, "27ABCDE1234F1Z5", "inv / 001", "2026-27") == \
        ids.invoice_id(TENANT, "27ABCDE1234F1Z5", "INV/001", "2026-27")
    with pytest.raises(ValidationError):
        _txn(tenant_id="not-a-uuid")
    with pytest.raises(ValidationError):
        _txn(tenant_id="0f8c3a52-7d1e-1b6a-9c2f-3e5d7a9b1c4e")  # version 1, not 4
    with pytest.raises(ValidationError):
        _txn(txn_uid="A" * 32)  # uppercase


def test_buyer_must_be_pan_keyed_and_not_owner_sourced():
    pan = "ABCCD1234E"
    ok = dict(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=f"27{pan}1Z5", name="X Ltd",
              type="listed", sector="textiles", sector_source="exchange", treds_mandated=None)
    T.Buyer(**ok)
    local = {**ok, "buyer_id": ids.local_buyer_id(TENANT, "X Ltd"), "id_basis": "local", "pan": None, "gstin": None}
    T.Buyer(**local)
    for bad in (dict(buyer_id="d" * 32), dict(gstin="27ZZZZZ9999Z1Z5"), dict(sector_source="owner"),
                dict(sector="unknown"), dict(sector_source="unknown"), dict(pan=None, gstin=None),
                dict(id_basis="local"), {**local, "gstin": f"27{pan}1Z5"}):
        with pytest.raises(ValidationError):
            T.Buyer(**{**ok, **bad})


def test_effective_sector_precedence(fx):
    pan = "ABCCD1234E"
    unknown = T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=None, name="X", type="unlisted", sector="unknown",
                      sector_source="unknown", treds_mandated=None)
    tb = T.TenantBuyer(tenant_id=TENANT, buyer_id=unknown.buyer_id, display_name=None, owner_sector="fmcg",
                       written_agreement=None, treds_onboarded=None)
    assert T.effective_sector(unknown, tb) == (E.Sector.FMCG, E.SectorSource.OWNER)
    assert T.effective_sector(unknown, None) == (E.Sector.UNKNOWN, E.SectorSource.UNKNOWN)
    listed = unknown.model_copy(update={"sector": E.Sector.PHARMA, "sector_source": E.SectorSource.EXCHANGE})
    assert T.effective_sector(listed, tb) == (E.Sector.PHARMA, E.SectorSource.EXCHANGE)


def test_disclosure_null_is_absent_not_zero(fx):
    df = fx["ref.buyer_disclosures"]
    for col in ("msme_not_due", "msme_unbilled", "msme_total_outstanding"):
        assert df[col].null_count() > 0, col
        assert df[col].drop_nulls().len() > 0, col


def test_event_payload_must_match_kind(fx):
    row = fx["ops.events"].row(0, named=True)
    with pytest.raises(ValidationError):
        T.Event(**{**row, "kind": "new_credit"})
    with pytest.raises(ValidationError):
        ev.Correction(correction_id="c", txn_uid="a" * 32, category="abstain")


def test_correction_cannot_be_abstain():
    with pytest.raises(ValidationError):
        T.TxnCorrection(tenant_id=TENANT, correction_id="c", txn_uid="a" * 32, category="abstain", input_id="i",
                        corrected_at=datetime(2026, 5, 1, tzinfo=UTC))


def test_overrides_hash_must_match(fx):
    row = fx["forecast.runs"].row(1, named=True)
    assert json.loads(row["overrides_json"])
    with pytest.raises(ValidationError, match="overrides_hash"):
        T.Run(**{**row, "overrides_hash": "0" * 64})


def test_seed_fits_uint64(fx):
    assert fx["forecast.runs"].schema["seed"] == pl.UInt64()
    assert fx["forecast.runs"]["seed"].max() >= 0


def test_shortfall_uncond_is_p_times_cond():
    row = dict(tenant_id=TENANT, run_id="R", date=date(2026, 6, 1), day=1, p_short=0.2,
               expected_shortfall_cond_paise=10_000, expected_shortfall_uncond_paise=2_000)
    T.Shortfall(**row)
    with pytest.raises(ValidationError):
        T.Shortfall(**{**row, "expected_shortfall_uncond_paise": 10_000})


def test_obligation_null_amount_needs_sales_forecaster():
    row = dict(tenant_id=TENANT, obligation_id="o", kind="rent", due_date=date(2026, 6, 1), amount_paise=None,
               confidence=0.9, amount_owner="obligations_detector")
    with pytest.raises(ValidationError):
        T.Obligation(**row)
    T.Obligation(**{**row, "kind": "gst", "amount_owner": "sales_forecaster"})
