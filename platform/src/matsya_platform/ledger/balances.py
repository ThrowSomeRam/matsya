"""ledger.balances rows: reconstructed per value date, and bank-reported from Summary.

Reconstructed closing(d) = opening + signed amounts with value_date <= d + accepted gaps
dated (by posting date) <= d. Rows are emitted only for dates that have transactions, and
only for accounts with an anchor (with none, the absolute balance is unknown).
continuity_ok is false on every date an accepted break could have made wrong: its
uncertainty window (continuity.Break.window_from through its posting date) and the
accept_gap's own window. True otherwise.

known_at of a reconstructed version is the fetch time of the build that produced it, the
moment the figure became known. It is not the latest known_at of the rows behind it: a
closing can change with no new row behind its date (an accepted gap resolved, an opening
re-derived), and that correction must be able to supersede the stale version.

Bank-reported rows take the Summary balance on the IST date of balanceDateTime, with
known_at = fetched_at of the carrying payload. One is written on every fetch, even when the
figure has not changed, so each fetch's view of the bank balance is on record.

The table is append-only. A reconstructed candidate becomes a new row only when its closing
or continuity_ok differs from the latest version for that account and date. On a key
collision (contract F1: the key has no balance_source) the bank-reported row wins.
"""

from matsya_contracts import tables as T
from matsya_contracts.enums import BalanceSource
from matsya_platform.ledger.continuity import Walk
from matsya_platform.ledger.dedup import LedgerRow
from matsya_platform.ledger.rebit import SummaryBalance


def reconstructed(rows: tuple[LedgerRow, ...], walks: dict[str, Walk], stamp) -> list[T.Balance]:
    out = []
    by_account: dict[str, list[LedgerRow]] = {}
    for r in rows:
        by_account.setdefault(r.account_id, []).append(r)
    for account_id, acc_rows in by_account.items():
        w = walks.get(account_id)
        if w is None or w.opening_paise is None:
            continue
        accepted = [b for b in w.breaks if b.accepted]
        tenant_id = acc_rows[0].txn.tenant_id
        for d in sorted({r.value_date for r in acc_rows}):
            behind = [r for r in acc_rows if r.value_date <= d]
            closing = w.opening_paise + sum(r.amount_paise for r in behind) + \
                sum(b.gap_paise for b in accepted if b.on <= d)
            in_gap = any(b.window_from <= d <= b.on or b.accepted_by.from_date <= d <= b.accepted_by.to_date
                         for b in accepted)
            out.append(T.Balance(tenant_id=tenant_id, account_id=account_id, date=d, closing_paise=closing,
                                 continuity_ok=not in_gap, balance_source=BalanceSource.RECONSTRUCTED,
                                 known_at=stamp))
    return out


def bank_reported(summaries: tuple[SummaryBalance, ...]) -> list[T.Balance]:
    return [T.Balance(tenant_id=s.tenant_id, account_id=s.account_id, date=s.ist_date, closing_paise=s.balance_paise,
                      continuity_ok=None, balance_source=BalanceSource.BANK_REPORTED, known_at=s.known_at)
            for s in sorted(summaries, key=lambda s: (s.known_at, s.payload_id))]


def new_versions(candidates: list[T.Balance], existing: tuple[T.Balance, ...]) -> list[T.Balance]:
    """Reconstructed candidates carry the build's fetch time (reconstructed(stamp)), so a
    changed figure always gets a key newer than the version it supersedes."""
    latest: dict[tuple, T.Balance] = {}
    keys = set()
    for b in sorted(existing, key=lambda b: b.known_at):
        latest[(b.tenant_id, b.account_id, b.date, b.balance_source)] = b
        keys.add((b.tenant_id, b.account_id, b.date, b.known_at))
    out = []
    for b in candidates:
        slot = (b.tenant_id, b.account_id, b.date, b.balance_source)
        prev = latest.get(slot)
        unchanged = prev is not None and (prev.closing_paise, prev.continuity_ok) == (b.closing_paise, b.continuity_ok)
        if unchanged and b.balance_source is BalanceSource.RECONSTRUCTED:
            continue
        key = (b.tenant_id, b.account_id, b.date, b.known_at)
        if key in keys:
            # Same fetch and date as a row already emitted. Callers pass bank-reported
            # candidates first, so the bank row wins (contract F1).
            continue
        latest[slot] = b
        keys.add(key)
        out.append(b)
    return out
