"""Contract rows -> ReBIT-shaped AA deposit JSON (one payload per fetch window).

Format only, no new facts:
- amount is an unsigned rupee string with a separate type (CREDIT/DEBIT);
- currentBalance is a rupee string: the running balance after the row in posting order
  (known_at, value_date, day_seq) from the account's opening balance;
- reference is the UTR (NEFT, RTGS) or RRN (IMPS, UPI) the narration already carries, as a
  bank fills that field; rows with none get no reference;
- transactionTimestamp is the row's known_at in IST (its posting time), valueDate its
  value_date;
- startDate and endDate are the window's declared range (windows.Window.start/end), which
  is what A4's coverage reads;
- Summary.currentBalance is the payload's own last row's running balance (or the balance
  carried in, for an empty window), at the window's end (balanceDateTime); drop_summary
  leaves the Summary out;
- txnId is the truth bank_txn_id.
ID faults remove fields per row (IdDrops); every rendered row records what it carried.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl

from render.windows import IST, Window

MODE = {"UPI": "UPI", "IMPS": "IMPS", "NEFT": "FT", "RTGS": "FT", "ATM": "ATM", "CARD": "CARD"}
REFERENCE = {
    "NEFT": re.compile(r"\b[A-Z]{4}N\d{13}\b"),
    "RTGS": re.compile(r"\b[A-Z]{4}R\d{16}\b"),
    "IMPS": re.compile(r"\b\d{12}\b"),
    "UPI": re.compile(r"\b\d{12}\b"),
}


@dataclass(frozen=True)
class IdDrops:
    txn_id: frozenset[str] = frozenset()  # drop_txn_id
    all_ids: frozenset[str] = frozenset()  # drop_all_ids: txnId, reference and currentBalance
    balances: frozenset[str] = frozenset()  # drop_balances

    def keeps_txn_id(self, uid: str) -> bool:
        return uid not in self.txn_id and uid not in self.all_ids

    def keeps_reference(self, uid: str) -> bool:
        return uid not in self.all_ids

    def keeps_balance(self, uid: str) -> bool:
        return uid not in self.balances and uid not in self.all_ids


@dataclass(frozen=True)
class Rendered:
    txn_uid: str  # truth id
    txn_id: str | None
    reference: str | None
    balance_after_paise: int | None


@dataclass
class PayloadOut:
    doc: dict
    rows: list[Rendered] = field(default_factory=list)
    summary_paise: int | None = 0  # None when the Summary is dropped

    @property
    def order(self) -> list[str]:
        return [r.txn_uid for r in self.rows]


def rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}{p // 100}.{p % 100:02d}"


def reference_of(narration: str | None, rail: str | None) -> str | None:
    rx = REFERENCE.get(rail or "")
    m = rx.search(narration or "") if rx else None
    return m.group() if m else None


def with_running_balance(rows: pl.DataFrame, opening: dict[str, int]) -> pl.DataFrame:
    """rows must already be in posting order (windows.with_posting)."""
    return rows.with_columns(
        (pl.col("account_id").replace_strict(opening, return_dtype=pl.Int64)
         + pl.col("amount_paise").cum_sum().over("account_id")).alias("balance_after_paise"))


def _ts(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat()


def payload_doc(window: Window, rows: pl.DataFrame, carried_in_paise: int, drops: IdDrops,
                omit_txn_uids: set[str], summary: bool = True) -> PayloadOut:
    """rows: this window's rows in posting order."""
    out = PayloadOut(doc={}, summary_paise=carried_in_paise)
    txns = []
    for r in rows.iter_rows(named=True):
        uid = r["txn_uid"]
        if uid in omit_txn_uids:
            continue
        txn_id = r["bank_txn_id"] if drops.keeps_txn_id(uid) else None
        ref = reference_of(r["narration_raw"], r["rail"]) if drops.keeps_reference(uid) else None
        bal = r["balance_after_paise"] if drops.keeps_balance(uid) else None
        t = {"txnId": txn_id} if txn_id else {}
        t |= {"type": "CREDIT" if r["amount_paise"] > 0 else "DEBIT", "mode": MODE.get(r["rail"], "OTHERS"),
              "amount": rupees(abs(r["amount_paise"]))}
        if bal is not None:
            t["currentBalance"] = rupees(bal)
        t |= {"transactionTimestamp": _ts(r["known_at"]), "valueDate": r["value_date"].isoformat(),
              "narration": r["narration_raw"] or ""}
        if ref:
            t["reference"] = ref
        txns.append(t)
        out.rows.append(Rendered(uid, txn_id, ref, bal))
        out.summary_paise = r["balance_after_paise"]
    acc = {"linkedAccRef": window.account_id,
           "maskedAccNumber": "XXXXXXXX" + window.account_id[-4:].upper().rjust(4, "X"), "type": "deposit"}
    if summary:
        acc["Summary"] = {"currentBalance": rupees(out.summary_paise), "currency": "INR",
                          "balanceDateTime": _ts(window.end_ts)}
    else:
        out.summary_paise = None
    acc["Transactions"] = {"startDate": window.start.isoformat(), "endDate": window.end.isoformat(), "Transaction": txns}
    out.doc = {"Account": acc}
    return out
