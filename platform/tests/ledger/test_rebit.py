from datetime import date, datetime, timezone

import pytest
from conftest import ACCOUNT, payload

from matsya_contracts.enums import Direction
from matsya_platform.ledger.rebit import RebitError, parse, to_paise


@pytest.mark.parametrize("text,paise", [("1200.50", 120050), ("15", 1500), ("0.05", 5), ("-200.00", -20000),
                                        ("100000.00", 10000000)])
def test_to_paise_is_exact(text, paise):
    assert to_paise(text) == paise


@pytest.mark.parametrize("text", ["1.234", "abc", "", "1e3"])
def test_to_paise_rejects_bad_amounts(text):
    with pytest.raises(RebitError):
        to_paise(text)


def test_parse_april():
    rows, summaries, ranges = parse(payload("april", "2026-05-01T02:00:00Z"))
    assert [r.bank_txn_id for r in rows] == ["A001", "A002", "A003", "A004"]
    assert [r.amount_paise for r in rows] == [500000, -120050, -1500, 250000]
    assert rows[1].direction is Direction.DEBIT and rows[1].raw_amount_paise == 120050
    assert rows[0].account_id == ACCOUNT and rows[0].value_date == date(2026, 4, 2)
    assert rows[0].balance_after_paise == 1500000
    assert all(r.known_at.isoformat() == "2026-05-01T02:00:00+00:00" for r in rows)
    (s,) = summaries
    assert s.balance_paise == 1628450 and s.ist_date == date(2026, 4, 30)
    (fr,) = ranges
    assert (fr.account_id, fr.start, fr.end) == (ACCOUNT, date(2026, 4, 1), date(2026, 4, 30))


def test_negative_debit_keeps_its_sign():
    rows, _, _ = parse(payload("sign_mismatch", "2026-05-01T02:00:00Z"))
    neg_debit = next(r for r in rows if r.bank_txn_id == "A104")
    assert neg_debit.amount_paise == -20000 and neg_debit.direction is Direction.DEBIT


def test_unknown_type_is_a_payload_error():
    p = payload("april", "2026-05-01T02:00:00Z")
    p.doc["Account"]["Transactions"]["Transaction"][0]["type"] = "REVERSAL"
    with pytest.raises(RebitError, match="type"):
        parse(p)


def test_every_row_of_a_fetch_shares_the_fetch_time():
    rows, summaries, _ = parse(payload("april", "2026-05-01T02:00:00Z"))
    assert {r.known_at for r in rows} | {s.known_at for s in summaries} == {datetime(2026, 5, 1, 2, tzinfo=timezone.utc)}


def test_numeric_amounts_and_balances_become_paise():
    rows, summaries, _ = parse(payload("summary_utc_numeric", "2026-05-01T02:00:00Z"))
    assert rows[0].amount_paise == 75025 and rows[0].balance_after_paise == 1075025
    assert summaries[0].balance_paise == 1075025


def test_summary_is_dated_by_ist():
    _, (s,), _ = parse(payload("summary_utc_numeric", "2026-05-01T02:00:00Z"))
    assert s.at.date() == date(2026, 4, 30)  # 19:00 UTC on 30 April ...
    assert s.ist_date == date(2026, 5, 1)  # ... is 00:30 IST on 1 May


def test_value_date_falls_back_to_the_ist_posting_date():
    p = payload("april", "2026-05-01T02:00:00Z")
    row = p.doc["Account"]["Transactions"]["Transaction"][0]
    del row["valueDate"]
    row["transactionTimestamp"] = "2026-04-02T20:00:00Z"  # 01:30 IST on 3 April
    rows, _, _ = parse(p)
    assert rows[0].value_date == date(2026, 4, 3)
