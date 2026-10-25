"""Rows A4 refuses to write: zero amounts and sign/direction mismatches.

Each refused row becomes an ops.dq_quarantine row and a dq_alert event. The batch goes on.
"""

from matsya_contracts import ids as cids
from matsya_contracts import tables as T
from matsya_contracts.enums import Direction, EventKind, QuarantineReason
from matsya_contracts.events import DqAlert
from matsya_platform.ledger.rebit import RawTxn


def reason(raw: RawTxn) -> QuarantineReason | None:
    if raw.raw_amount_paise == 0:
        return QuarantineReason.ZERO_AMOUNT
    if raw.raw_amount_paise < 0 and raw.direction is Direction.CREDIT:
        return QuarantineReason.SIGN_DIRECTION_MISMATCH
    return None


def screen(rows: list[RawTxn]) -> tuple[list[RawTxn], list[T.DqQuarantine], list[T.Event]]:
    clean, quarantined, events = [], [], []
    for raw in rows:
        why = reason(raw)
        if why is None:
            clean.append(raw)
            continue
        q = T.DqQuarantine(
            tenant_id=raw.tenant_id, quarantine_id=cids.hash32("quarantine", raw.payload_id, raw.account_id, str(raw.index)),
            payload_id=raw.payload_id, account_id=raw.account_id, reason=why, value_date=raw.value_date,
            amount_paise=raw.raw_amount_paise, direction=raw.direction, narration_raw=raw.narration,
            known_at=raw.known_at,
        )
        quarantined.append(q)
        alert = DqAlert(quarantine_id=q.quarantine_id, reason=why, payload_id=raw.payload_id, account_id=raw.account_id)
        events.append(T.Event(tenant_id=raw.tenant_id, event_id=cids.hash32("event", "dq_alert", q.quarantine_id),
                              kind=EventKind.DQ_ALERT, payload_json=alert.model_dump_json(), at=raw.known_at))
    return clean, quarantined, events
