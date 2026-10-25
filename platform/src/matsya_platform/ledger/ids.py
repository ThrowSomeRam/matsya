"""Choose each row's txn_uid key path. The key formats and hashing live in contracts' ids.py.

Path order: bank txnId, then UTR/RRN (the row's reference), then the post-transaction
running balance, then an ordinal among identical rows of the same payload.

One UTR/RRN key per payload: a second row with the same reference and amount in the same
payload is a second transaction (two identical Rs 500 credits from one payer), so it falls
through to its own running-balance or ordinal key. Merging them would lose money, which
cannot be recovered; a false duplicate stays visible.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from matsya_contracts import ids as cids
from matsya_platform.ledger.rebit import RawTxn

KeyPath = Literal["txn", "utr", "bal", "ord"]


@dataclass(frozen=True)
class Keyed:
    raw: RawTxn
    key_path: KeyPath
    txn_uid: str


def _key(raw: RawTxn, ordinal: int, utr_taken: bool) -> tuple[KeyPath, str]:
    amount = raw.amount_paise
    if raw.bank_txn_id:
        return "txn", cids.txn_key_bank_id(raw.bank_txn_id, raw.value_date, amount)
    if raw.reference and not utr_taken:
        return "utr", cids.txn_key_utr(raw.reference, amount)
    if raw.balance_after_paise is not None:
        return "bal", cids.txn_key_balance(raw.balance_after_paise, raw.value_date, amount)
    return "ord", cids.txn_key_ordinal(raw.value_date, amount, raw.narration or "", ordinal)


def assign(rows: list[RawTxn]) -> list[Keyed]:
    """Key rows of one or more payloads. Ordinals count within a payload only."""
    seen: Counter = Counter()
    utrs: set[tuple] = set()
    out = []
    for raw in rows:
        utr = (raw.payload_id, raw.account_id, raw.reference, raw.amount_paise) if raw.reference else None
        utr_taken = utr in utrs
        if utr and not raw.bank_txn_id and not utr_taken:
            utrs.add(utr)
        ordinal = 0
        if not (raw.bank_txn_id or (raw.reference and not utr_taken) or raw.balance_after_paise is not None):
            ident = (raw.payload_id, raw.account_id, raw.value_date, raw.amount_paise,
                     cids.normalise_party(raw.narration or ""))
            ordinal = seen[ident]
            seen[ident] += 1
        path, key = _key(raw, ordinal, utr_taken)
        out.append(Keyed(raw=raw, key_path=path, txn_uid=cids.txn_uid(raw.tenant_id, raw.account_id, key)))
    return out
