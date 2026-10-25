from datetime import date, datetime, timezone

import polars as pl
import pytest
from conftest import ACCOUNT, payload

from matsya_contracts.enums import BalanceSource, EventKind, QuarantineReason
from matsya_contracts.events import AcceptGap, TxnIdConflict, parse_payload
from matsya_platform.ledger.build import LedgerState, as_of_cutoff, build, validate_outputs
from matsya_platform.ledger.continuity import ContinuityError

APR = ("april", "2026-05-01T02:00:00Z")
MAY = ("may", "2026-06-01T02:00:00Z")


def run(*specs, state: LedgerState | None = None):
    result = build([payload(*s) for s in specs], state or LedgerState())
    validate_outputs(result, sample=None)  # full validation in tests
    return result


def latest(balances: pl.DataFrame, as_of: datetime | None = None) -> pl.DataFrame:
    df = balances if as_of is None else balances.filter(pl.col("known_at") <= as_of)
    return (df.sort("known_at").group_by("account_id", "date", "balance_source", maintain_order=True).last()
            .select("date", "balance_source", "closing_paise", "continuity_ok").sort("date", "balance_source"))


def txns(state: LedgerState) -> pl.DataFrame:
    return state.transactions_frame().sort("txn_uid")


def closing(balances: pl.DataFrame, d: date, as_of: datetime | None = None) -> int:
    df = latest(balances, as_of).filter((pl.col("date") == d) & (pl.col("balance_source") == "reconstructed"))
    return df["closing_paise"].item()


# ---------------------------------------------------------------- the original six


def test_identical_same_day_credits_stay_separate():
    with_balance = run(("identical_same_day", "2026-04-04T02:00:00Z"))
    assert with_balance.txns.height == 2 and with_balance.txns["txn_uid"].n_unique() == 2
    no_balance = run(("identical_no_balance", "2026-04-04T02:00:00Z"))
    assert no_balance.txns.height == 2 and no_balance.txns["txn_uid"].n_unique() == 2
    assert no_balance.txns["dup_group_id"].null_count() == 2  # same payload: never suspects


def test_duplicate_payload_is_a_noop():
    first = run(APR)
    again = run(APR, state=first.state)
    assert again.txns.height == 0 and again.balances.height == 0 and again.events.height == 0
    same_call = run(APR, ("april", "2026-05-01T02:00:00Z", "PL-april-redelivered"))
    assert same_call.txns.height == 4


def test_out_of_order_payloads_reach_the_same_ledger():
    in_order = run(APR, MAY).state
    reversed_ = run(APR, state=run(MAY).state).state
    assert txns(in_order).equals(txns(reversed_))
    assert latest(in_order.balances_frame()).equals(latest(reversed_.balances_frame()))


def test_sign_mismatch_and_zero_amount_are_quarantined():
    r = run(("sign_mismatch", "2026-05-01T02:00:00Z"))
    assert sorted(r.txns["amount_paise"]) == [-20000, 100000]
    assert set(r.quarantine["reason"]) == {QuarantineReason.ZERO_AMOUNT, QuarantineReason.SIGN_DIRECTION_MISMATCH}
    bad = r.quarantine.filter(pl.col("reason") == "sign_direction_mismatch").row(0, named=True)
    assert bad["amount_paise"] == -50000 and bad["direction"] == "credit"
    alerts = r.events.filter(pl.col("kind") == EventKind.DQ_ALERT)
    assert alerts.height == 2
    assert {parse_payload(k, p).quarantine_id for k, p in alerts.select("kind", "payload_json").iter_rows()} == \
        set(r.quarantine["quarantine_id"])


def test_continuity_break_fails_the_batch():
    prior = run(APR).state
    rows_before = prior.rows
    with pytest.raises(ContinuityError) as e:
        build([payload("continuity_break", "2026-05-01T03:00:00Z")], LedgerState())
    (brk,) = e.value.breaks
    assert brk.account_id == ACCOUNT and brk.on == date(2026, 4, 3) and brk.gap_paise == 50000
    with pytest.raises(ContinuityError):
        build([payload("continuity_break", "2026-05-01T03:00:00Z")], prior)
    assert prior.rows is rows_before  # nothing written: the state is untouched


def test_late_txn_appends_balance_rows():
    before = run(APR, MAY).state
    r = run(("late_backdated", "2026-06-03T02:00:00Z"), state=before)
    assert r.txns.height == 1
    new = r.balances.filter(pl.col("balance_source") == BalanceSource.RECONSTRUCTED)
    assert sorted(new["date"]) == [date(2026, 4, 25), date(2026, 5, 3), date(2026, 5, 10)]
    old = before.balances_frame()
    assert set(old.rows()) <= set(r.state.balances_frame().rows())  # old versions untouched
    bal = r.state.balances_frame()
    first_known = datetime.fromisoformat(MAY[1])
    assert closing(bal, date(2026, 5, 10), first_known) == 1308450
    assert closing(bal, date(2026, 5, 10)) == 1338450
    assert closing(bal, date(2026, 4, 20)) == 1628450  # unchanged date, no new version


def test_overlapping_window_with_ordinal_keys_drops_the_redelivery():
    together = run(("overlap_a", "2026-04-09T02:00:00Z"), ("overlap_b", "2026-04-16T02:00:00Z"))
    assert sorted(together.txns["amount_paise"]) == [-30000, 100000, 200000]
    in_turn = run(("overlap_b", "2026-04-16T02:00:00Z"), state=run(("overlap_a", "2026-04-09T02:00:00Z")).state)
    assert txns(in_turn.state).equals(txns(together.state))


def test_unresolvable_suspect_is_kept_and_flagged():
    r = run(("overlap_a", "2026-04-09T02:00:00Z"), ("overlap_b_no_summary", "2026-04-16T02:00:00Z"))
    assert r.txns.height == 4
    flagged = r.txns.filter(pl.col("dup_group_id").is_not_null())
    assert flagged.height == 2 and flagged["dup_group_id"].n_unique() == 1
    assert (flagged["dq_score"] < 1.0).all()


# ---------------------------------------------------------------- key-path collisions


def test_reused_txn_id_keeps_both_rows_and_reports_conflict():
    first = run(APR)
    r = run(("reused_txnid", "2026-06-01T02:00:00Z"), state=first.state)
    assert r.txns.height == 1
    assert txns(r.state).height == 5
    (conflict,) = r.events.filter(pl.col("kind") == EventKind.TXN_ID_CONFLICT).select("kind", "payload_json").iter_rows()
    p = parse_payload(*conflict)
    assert isinstance(p, TxnIdConflict) and p.bank_txn_id == "A001"
    assert p.existing_amount_paise == 500000 and p.new_amount_paise == 70000


def test_utr_covering_payment_and_charge_stays_two_rows():
    r = run(("utr_charge", "2026-05-01T02:00:00Z"))
    assert sorted(r.txns["amount_paise"]) == [-1180, 10000000]


def test_same_running_balance_on_two_dates_stays_two_rows():
    r = run(("same_balance", "2026-05-01T02:00:00Z"))
    assert r.txns.height == 3
    assert r.txns.filter(pl.col("amount_paise") == 10000)["txn_uid"].n_unique() == 2


# ---------------------------------------------------------------- accept_gap


def _gap(gap_paise: int) -> AcceptGap:
    return AcceptGap(account_id=ACCOUNT, from_date=date(2026, 4, 3), to_date=date(2026, 4, 3), gap_paise=gap_paise,
                     accepted_by="operator", note="bank confirms missing statement row")


def test_accept_gap_lets_the_break_through():
    r = run(("continuity_break", "2026-05-01T03:00:00Z"), state=LedgerState(accepted_gaps=(_gap(50000),)))
    assert r.txns.height == 2
    dq = dict(r.txns.select("amount_paise", "dq_score").iter_rows())
    assert dq[100000] == 1.0 and dq[-20000] < 1.0
    rec = latest(r.balances).filter(pl.col("balance_source") == "reconstructed")
    # The missing row was posted after B001's anchor and before B003's, so every date from
    # B001's posting date to the break may be wrong: none of them is marked continuous.
    assert dict(rec.select("date", "continuity_ok").iter_rows()) == {date(2026, 4, 1): False, date(2026, 4, 3): False}
    assert closing(r.balances, date(2026, 4, 3)) == 1130000  # re-anchored to the bank


def test_accept_gap_must_match_the_gap_amount():
    with pytest.raises(ContinuityError):
        run(("continuity_break", "2026-05-01T03:00:00Z"), state=LedgerState(accepted_gaps=(_gap(40000),)))


@pytest.mark.xfail(strict=True, reason="contract F1: ledger.balances key lacks balance_source (positions doc)")
def test_bank_reported_and_reconstructed_on_the_same_date_both_kept():
    r = run(("overlap_a", "2026-04-09T02:00:00Z"))
    on_8th = r.balances.filter(pl.col("date") == date(2026, 4, 8))
    assert set(on_8th["balance_source"]) == {"reconstructed", "bank_reported"}


# ---------------------------------------------------------------- AA model rules


def test_bank_reported_row_on_every_fetch():
    first = run(APR)
    again = run(("april_late_fetch", "2026-05-02T02:00:00Z"), state=first.state)
    assert again.txns.height == 0  # same rows: nothing new in the ledger
    bank = again.balances.filter(pl.col("balance_source") == "bank_reported")
    assert bank.height == 1 and bank["known_at"][0] == datetime(2026, 5, 2, 2, tzinfo=timezone.utc)
    assert again.balances.filter(pl.col("balance_source") == "reconstructed").height == 0


def test_bank_reported_row_is_dated_by_ist_and_wins_a_key_collision():
    r = run(("summary_utc_numeric", "2026-05-01T02:00:00Z"))
    bank = r.balances.filter(pl.col("balance_source") == "bank_reported")
    assert bank["date"].to_list() == [date(2026, 5, 1)] and bank["closing_paise"][0] == 1075025
    # overlap_a: the summary and the last txn share 8 April and the fetch time. Bank wins.
    on_8th = run(("overlap_a", "2026-04-09T02:00:00Z")).balances.filter(pl.col("date") == date(2026, 4, 8))
    assert on_8th["balance_source"].to_list() == ["bank_reported"]


def _without_backdated(p):
    doc = {"Account": {**p.doc["Account"], "Transactions": {**p.doc["Account"]["Transactions"], "Transaction": []}}}
    return type(p)(tenant_id=p.tenant_id, payload_id=p.payload_id, fetched_at=p.fetched_at, doc=doc)


def test_backdated_row_lands_at_its_value_date_without_moving_coverage():
    before = run(APR, MAY).state
    late = payload("late_backdated", "2026-06-03T02:00:00Z")
    r = build([late], before)
    validate_outputs(r, sample=None)
    (row,) = r.txns.iter_rows(named=True)
    assert row["value_date"] == date(2026, 4, 25) and row["known_at"] == late.fetched_at
    without = build([_without_backdated(late)], before)
    assert r.coverage.equals(without.coverage)  # coverage comes from fetch ranges, not row dates
    assert r.coverage["complete_through"].to_list() == [date(2026, 6, 2)]


def test_coverage_stops_at_a_gap_between_fetch_ranges():
    june = _without_backdated(payload("late_backdated", "2026-06-03T02:00:00Z"))  # range 1-2 June, no rows
    r = build([june], run(APR).state)  # April, then June: May was never fetched
    assert r.coverage["complete_through"].to_list() == [date(2026, 4, 30)]


def test_as_of_cutoff_is_end_of_day_ist():
    assert as_of_cutoff(date(2026, 5, 1)) == datetime(2026, 5, 1, 18, 29, 59, 999999, tzinfo=timezone.utc)


def test_as_of_read_excludes_a_fetch_after_the_ist_day_boundary():
    inside = run(("april", "2026-05-01T18:29:59.999999Z"))
    after = run(("may", "2026-05-01T18:30:00Z"), state=inside.state)
    view = after.state.as_of(date(2026, 5, 1))
    assert view["transactions"].height == 4  # May's fetch is 00:00 IST on 2 May
    assert view["transactions"]["known_at"].max() == datetime(2026, 5, 1, 18, 29, 59, 999999, tzinfo=timezone.utc)
    assert (view["balances"]["known_at"] <= as_of_cutoff(date(2026, 5, 1))).all()
    assert after.state.as_of(date(2026, 5, 2))["transactions"].height == 6


# ---------------------------------------------------------------- balance versioning (sweep regression)

P1 = ("versioning_p1", "2025-10-28T00:30:00Z")
P2 = ("versioning_p2", "2025-11-04T00:30:00Z")
# True closings by value date once R (+500, value-dated 26 Oct) is back.
TRUE_CLOSINGS = {date(2025, 10, 21): 1010000, date(2025, 10, 24): 1005000, date(2025, 10, 25): 1003000,
                 date(2025, 10, 26): 1053000, date(2025, 11, 1): 1054000}


def _recon(balances: pl.DataFrame) -> dict:
    rec = latest(balances).filter(pl.col("balance_source") == "reconstructed")
    return {d: (c, ok) for d, _, c, ok in rec.iter_rows()}


def test_gap_break_is_dated_by_posting_date():
    with pytest.raises(ContinuityError) as e:
        build([payload(*P1)], LedgerState())
    (brk,) = e.value.breaks
    # Y is posted on 26 Oct but value-dated 24 Oct; the gap it reveals arose in posting order.
    assert brk.on == date(2025, 10, 26) and brk.gap_paise == 50000


def test_corrected_closing_supersedes_a_stale_one():
    gap = AcceptGap(account_id=ACCOUNT, from_date=date(2025, 10, 26), to_date=date(2025, 10, 26), gap_paise=50000,
                    accepted_by="operator", note="R missing from the fetch")
    first = run(P1, state=LedgerState(accepted_gaps=(gap,)))
    for d, (closing, ok) in _recon(first.state.balances_frame()).items():
        if closing != TRUE_CLOSINGS.get(d, closing):
            assert ok is False, f"{d}: wrong closing {closing} marked continuous"
    second = run(P2, state=first.state)
    assert _recon(second.state.balances_frame()) == {d: (c, True) for d, c in TRUE_CLOSINGS.items()}
    v25 = second.state.balances_frame().filter((pl.col("date") == date(2025, 10, 25))
                                               & (pl.col("balance_source") == "reconstructed")).sort("known_at")
    assert v25["known_at"][-1] == datetime.fromisoformat(P2[1])  # the correction carries the fetch that made it


# ---------------------------------------------------------------- unverified payloads, UTR twins


def test_unverified_payload_is_stored_and_flagged():
    r = run(("unverified", "2025-04-08T00:30:00Z"))
    assert r.txns.height == 2 and (r.txns["dq_score"] < 1.0).all()
    (u,) = r.unverified.iter_rows(named=True)
    assert u["payload_id"] == "PL-unverified" and u["account_id"] == ACCOUNT and u["rows"] == 2


def test_verified_payload_is_not_flagged():
    r = run(APR)
    assert r.unverified.height == 0 and (r.txns["dq_score"] == 1.0).all()


@pytest.mark.parametrize("name", ["utr_twins", "utr_twins_no_balance"])
def test_rows_sharing_utr_and_amount_stay_two_transactions(name):
    r = run((name, "2025-04-08T00:30:00Z"))
    assert r.txns.height == 2 and r.txns["txn_uid"].n_unique() == 2
    paths = sorted(row.key_path for row in r.state.rows)
    assert paths == (["bal", "utr"] if name == "utr_twins" else ["ord", "utr"])
