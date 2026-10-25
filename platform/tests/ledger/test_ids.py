from conftest import ACCOUNT, TENANT, payload

from matsya_contracts import ids as cids
from matsya_platform.ledger.ids import assign
from matsya_platform.ledger.rebit import parse


def _keyed(name):
    rows, _, _ = parse(payload(name, "2026-05-01T02:00:00Z"))
    return assign(rows)


def test_txn_id_path_uses_contract_builder():
    k = _keyed("april")[0]
    assert k.key_path == "txn"
    want = cids.txn_uid(TENANT, ACCOUNT, cids.txn_key_bank_id("A001", k.raw.value_date, 500000))
    assert k.txn_uid == want


def test_utr_path():
    credit, charge = _keyed("utr_charge")
    assert credit.key_path == charge.key_path == "utr"
    assert credit.txn_uid == cids.txn_uid(TENANT, ACCOUNT, cids.txn_key_utr("UTR998877", 10000000))
    assert credit.txn_uid != charge.txn_uid


def test_balance_path():
    first, _, third = _keyed("same_balance")
    assert first.key_path == "bal"
    assert first.txn_uid == cids.txn_uid(TENANT, ACCOUNT, cids.txn_key_balance(1010000, first.raw.value_date, 10000))
    assert first.txn_uid != third.txn_uid


def test_ordinal_path_counts_identical_rows_in_a_payload():
    a, b = _keyed("identical_no_balance")
    assert a.key_path == b.key_path == "ord"
    assert a.txn_uid == cids.txn_uid(TENANT, ACCOUNT, cids.txn_key_ordinal(a.raw.value_date, 50000, "UPI/CR/RAVI", 0))
    assert b.txn_uid == cids.txn_uid(TENANT, ACCOUNT, cids.txn_key_ordinal(a.raw.value_date, 50000, "UPI/CR/RAVI", 1))
