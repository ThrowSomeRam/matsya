"""Every contract table Track B reads or writes validates against its schema, and the
contract rules (as-of reads, TReDS censoring, unknown terms) hold in the adapters."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest
from matsya_contracts.fixtures import generate as contract_fixtures
from matsya_contracts.tables import TABLES
from matsya_contracts.validate import ContractError, validate

from matsya_intelligence.io import adapters
from matsya_intelligence.io.sink import write_outputs
from matsya_intelligence.io.source import load, read_contract_dir
from matsya_intelligence.io.types import READS, WRITES
from matsya_intelligence.run import run_all, run_tenant
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.export import TRUTH_TABLES, write_contract_parquet
from matsya_intelligence.timing.labels import timing_labels

N_PATHS = 500


@pytest.fixture(scope="module")
def world():
    cfg = WorldConfig(treds_discount_prob=0.5, terms_unknown_share=0.2)
    return cfg, generate_world(cfg, past_seed=0, future_seed=0)


@pytest.fixture(scope="module")
def outputs():
    return run_all(load({"DATA_SOURCE": "synth", "synth": {"preset": "borderline", "past_seed": 0, "future_seed": 0}}),
                   N_PATHS)


# ---------------------------------------------------------------- reads


@pytest.mark.parametrize("table", READS)
def test_generator_tables_validate(world, table):
    _, (tables, _) = world
    validate(tables[table], table)  # every row


@pytest.mark.parametrize("seed", [0, 1])
def test_adapters_accept_track_a_fixtures(seed):
    """Track A's contract fixtures go through every adapter for every tenant."""
    fx = contract_fixtures(seed)
    as_of = date(2026, 4, 1) + timedelta(days=60)
    for tenant in fx["ledger.transactions"]["tenant_id"].unique():
        t = adapters.for_tenant(fx, tenant)
        inp = adapters.track_b_inputs(t, as_of)
        assert inp["invoices"]["terms_days"].null_count() == 0
        assert inp["buyers"].height == fx["ref.tenant_buyers"].filter(pl.col("tenant_id") == tenant).height


def test_write_contract_parquet_round_trip(world, tmp_path):
    _, w = world
    paths = write_contract_parquet(w, tmp_path)
    assert {f"truth.{n}" for n in TRUTH_TABLES} <= set(paths)
    assert all((tmp_path / "truth" / f"{n}.parquet").exists() for n in TRUTH_TABLES)
    back = read_contract_dir(tmp_path, sample=None)
    for name in READS:
        assert back[name].equals(w[0][name]), name


def test_data_source_switch_is_bit_identical(tmp_path):
    """The same world read from the generator or from contract Parquet gives the same outputs."""
    cfg = WorldConfig()
    w = generate_world(cfg, past_seed=1, future_seed=0)
    write_contract_parquet(w, tmp_path)
    synth = load({"DATA_SOURCE": "synth", "synth": {"preset": "borderline", "past_seed": 1, "future_seed": 0}})
    contract = load({"DATA_SOURCE": "contract", "contract": {"dir": str(tmp_path), "as_of": None}})
    tenant = cfg.tenant_id
    a = run_tenant(synth.tables, tenant, cfg.as_of_date, "same", N_PATHS)
    b = run_tenant(contract.tables, tenant, contract.as_of, "same", N_PATHS)
    assert set(a) == set(b)
    for k in a:
        assert a[k].equals(b[k]), k


# ---------------------------------------------------------------- writes


def test_every_output_table_is_written_and_validates(outputs, tmp_path):
    assert set(outputs) == set(WRITES)
    for name, df in outputs.items():
        assert df.height > 0, name
        validate(df, name)  # every row
    assert len(write_outputs(outputs, tmp_path)) == len(WRITES)


def test_sink_refuses_track_a_tables(world, tmp_path):
    _, (tables, _) = world
    with pytest.raises(ValueError):
        write_outputs({"ledger.transactions": tables["ledger.transactions"]}, tmp_path)


def test_sink_refuses_invalid_rows(outputs, tmp_path):
    bad = outputs["forecast.shortfall"].with_columns(pl.lit(1.5).alias("p_short"))
    with pytest.raises(ContractError):
        write_outputs({"forecast.shortfall": bad}, tmp_path, sample=None)


def test_writes_are_track_b_tables():
    assert all(TABLES[t].writer == "B" for t in WRITES)
    assert all(TABLES[t].writer == "A" for t in READS)


# ---------------------------------------------------------------- contract rules


def test_as_of_read_takes_latest_known_version():
    """The fixtures hold an open invoice that is paid later: before that known_at it reads open."""
    fx = contract_fixtures(0)
    inv = fx["ledger.invoices"]
    later = inv.filter(pl.col("invoice_id").is_duplicated()).sort("known_at")
    iid, k_paid = later["invoice_id"][-1], later["known_at"][-1]
    before = adapters.as_of_read(inv, ["tenant_id", "invoice_id"], k_paid - timedelta(seconds=1))
    after = adapters.as_of_read(inv, ["tenant_id", "invoice_id"], k_paid)
    assert before.filter(pl.col("invoice_id") == iid)["status"][0] == "open"
    assert after.filter(pl.col("invoice_id") == iid)["status"][0] == "paid"


def test_treds_financier_censors_at_discount_date(world):
    cfg, (tables, truth) = world
    disc = truth.invoices.filter(pl.col("treds_discount_date").is_not_null()
                                 & (pl.col("treds_discount_date") <= cfg.as_of_date - timedelta(days=3)))
    assert disc.height > 0
    out = adapters.outcomes(tables, cfg.as_of)
    inv = adapters.invoices(tables, cfg.as_of)
    j = disc.select("invoice_id", "treds_discount_date").join(out, on="invoice_id")
    assert j.height == disc.height
    assert (j["censored_at"] == j["treds_discount_date"]).all()  # censored for timing
    assert j["paid_date"].null_count() == j.height
    assert (inv.filter(pl.col("invoice_id").is_in(disc["invoice_id"].implode()))["status"] == "paid").all()
    # The cash is real: the financier's credit is in the ledger.
    m = tables["ledger.matches"].filter(pl.col("payer_kind") == "treds_financier")
    assert m["txn_uid"].is_in(tables["ledger.transactions"]["txn_uid"].implode()).all()
    # Timing labels: open at the full weeks to the discount date, never paid.
    lab = timing_labels(out, inv, cfg.as_of_date).join(j.select("invoice_id", "treds_discount_date", "issue_date"),
                                                        on="invoice_id")
    assert (lab["state"] == "open").all()
    assert (lab["c"] == ((lab["treds_discount_date"] - lab["issue_date"]).dt.total_days() // 7)).all()


def test_treds_rule_comes_from_matches_even_if_label_disagrees(world):
    """A treds_financier match censors the invoice even when the label row says settled by buyer."""
    cfg, (tables, _) = world
    iid = tables["ledger.matches"].filter(pl.col("payer_kind") == "treds_financier")["invoice_id"][0]
    relabel = tables["labels.invoice_outcomes"].with_columns(
        pl.when(pl.col("invoice_id") == iid).then(pl.lit(None, pl.Date)).otherwise(pl.col("censored_at"))
        .alias("censored_at"),
        pl.when((pl.col("invoice_id") == iid) & pl.col("paid_date").is_not_null()).then(pl.lit("buyer"))
        .otherwise(pl.col("settlement_kind")).alias("settlement_kind"))
    out = adapters.outcomes({**tables, "labels.invoice_outcomes": relabel}, cfg.as_of)
    row = out.filter(pl.col("invoice_id") == iid)
    assert row["censored_at"][0] is not None and row["paid_date"][0] is None


@pytest.mark.parametrize("source, terms, expected", [
    ("unknown", None, (45, 45)),
    ("unknown", 30, (45, 45)),
    ("default_15d", None, (15, 15)),
    ("document_terms", 30, (30, 30)),
    ("document_terms", 60, (60, 45)),
    ("written_agreement", 90, (90, 45)),
    ("document_terms", None, (45, 45)),
    ("some_future_member", 30, (45, 45)),
])
def test_resolve_terms(source, terms, expected):
    assert adapters.resolve_terms(source, terms) == expected


def test_unknown_terms_default_to_45_days(world):
    cfg, (tables, _) = world
    raw = tables["ledger.invoices"].filter(pl.col("terms_source") == "unknown")
    assert raw.height > 0 and raw["terms_days"].null_count() == raw.height
    inv = adapters.invoices(tables, cfg.as_of).filter(pl.col("terms_source") == "unknown")
    assert (inv["terms_days"] == 45).all() and (inv["msmed_deadline_days"] == 45).all()
    assert ((inv["due_date"] - inv["issue_date"]).dt.total_days() == 45).all()


def test_timing_labels_use_known_at_not_a_lag():
    """A payment known by as_of counts as paid even one day before as_of; no 3-day cut."""
    as_of = date(2026, 3, 31)
    out = pl.DataFrame({
        "tenant_id": ["t"] * 3, "invoice_id": ["a", "b", "c"], "buyer_id": ["x"] * 3,
        "issue_date": [date(2026, 3, 2)] * 3,
        "paid_date": [date(2026, 3, 29), None, None],  # a: week 4, ends 29 Mar, inside c_full = 4
        "paid_fraction": [1.0, 0.0, 1.0],
        "censored_at": [None, as_of, date(2026, 3, 10)],  # b: open; c: TReDS discount
    })
    inv = out.select("invoice_id", pl.col("issue_date").alias("due_date"))
    lab = timing_labels(out, inv, as_of).sort("invoice_id")
    assert lab["state"].to_list() == ["paid", "open", "open"]
    assert lab["k"][0] == 4
    assert lab["c"].to_list()[1:] == [4, 1]


def test_transactions_known_after_as_of_are_invisible(world):
    cfg, (tables, truth) = world
    early = datetime.combine(cfg.as_of_date - timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc)
    t = adapters.transactions(tables, early)
    assert t["known_at"].max() <= early
    assert t.height < tables["ledger.transactions"].height
    out = adapters.outcomes(tables, early)
    assert out["issue_date"].max() <= early.date()
    assert np.all(out["censored_at"].drop_nulls().to_numpy() <= np.datetime64(early.date()))


def test_opening_balance_sums_each_accounts_own_latest_date(world):
    """An account last updated a day earlier still counts; the date is the stalest account's."""
    cfg, (tables, _) = world
    bal = tables["ledger.balances"]
    d = cfg.as_of_date
    other = (bal.filter(pl.col("date") <= d - timedelta(days=1))
             .with_columns(pl.lit("acc_second").alias("account_id"), pl.lit(5_00_000_00, pl.Int64).alias("closing_paise")))
    two = {**tables, "ledger.balances": pl.concat([bal, other])}
    validate(two["ledger.balances"], "ledger.balances")
    day1, one_acc, _ = adapters.opening_balance(tables, cfg.as_of)
    day2, both, _ = adapters.opening_balance(two, cfg.as_of)
    assert day1 == d and day2 == d - timedelta(days=1)
    assert both == one_acc + 5_00_000_00


def test_datetime_as_of_is_not_widened_to_end_of_day(world):
    """A datetime as_of reads exactly to that instant, through to the engine inputs."""
    from matsya_intelligence.engine import build_learned_inputs

    cfg, (tables, truth) = world
    noon = datetime.combine(cfg.as_of_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    late = tables["ledger.transactions"].filter(pl.col("known_at") > noon)
    assert late.height > 0  # rows known between noon and the world's 18:00 cut
    assert adapters.as_of_cutoff(noon) == noon
    cats = truth.transactions.select("txn_uid", "category")
    at_noon = build_learned_inputs(tables, cats, noon, "dt", sales_uncertainty=False)
    end_of_day = build_learned_inputs(tables, cats, cfg.as_of_date, "dt", sales_uncertainty=False)
    snap = lambda inp: dict(s.split(":", 1) for s in inp.input_snapshot_ids)
    assert snap(at_noon)["transactions"] != snap(end_of_day)["transactions"]
    assert at_noon.as_of == end_of_day.as_of == cfg.as_of_date
