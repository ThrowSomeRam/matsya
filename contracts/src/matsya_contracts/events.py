"""Typed payloads for ops.events, one model per EventKind. Track B reads only these kinds."""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from matsya_contracts.enums import ConsentStatus, EventKind, OverrideKind, QuarantineReason, TxnCategory
from matsya_contracts.ids import BuyerId, InvoiceId, NonEmpty, Sha256Hex, TxnUid


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OverrideItem(Payload):
    """One owner edit to a run's simulation inputs. Matches the engine's OneOffEvent.

    amount_paise is signed: + inflow, - outflow.
    """

    kind: OverrideKind
    on: date
    amount_paise: int


OVERRIDE_LIST = TypeAdapter(list[OverrideItem])


class ConsentChanged(Payload):
    consent_id: NonEmpty
    status: ConsentStatus
    previous: ConsentStatus | None


class PayloadFetched(Payload):
    payload_id: NonEmpty
    consent_id: NonEmpty
    fip_id: NonEmpty
    sha256: Sha256Hex


class FetchFailed(Payload):
    consent_id: NonEmpty
    fip_id: NonEmpty
    attempt: Annotated[int, Field(ge=1)]
    error: NonEmpty


class NewCredit(Payload):
    txn_uid: TxnUid
    account_id: NonEmpty
    value_date: date
    amount_paise: Annotated[int, Field(gt=0)]


class NewInvoice(Payload):
    invoice_id: InvoiceId
    buyer_id: BuyerId
    issue_date: date
    gross_paise: Annotated[int, Field(gt=0)]


class Correction(Payload):
    correction_id: NonEmpty
    txn_uid: TxnUid
    category: TxnCategory

    @model_validator(mode="after")
    def _not_abstain(self):
        if self.category is TxnCategory.ABSTAIN:
            raise ValueError("a correction cannot be abstain")
        return self


class Override(Payload):
    items: list[OverrideItem]


class DqAlert(Payload):
    quarantine_id: NonEmpty
    reason: QuarantineReason
    payload_id: NonEmpty
    account_id: NonEmpty


class AcceptGap(Payload):
    """An operator accepts a balance-continuity break so the account can rebuild."""

    account_id: NonEmpty
    from_date: date
    to_date: date
    gap_paise: int  # signed: bank-reported minus reconstructed
    accepted_by: NonEmpty
    note: str

    @model_validator(mode="after")
    def _ordered(self):
        if self.to_date < self.from_date:
            raise ValueError("to_date before from_date")
        return self


class TxnIdConflict(Payload):
    """A bank txnId arrived again with different content (reused or corrupted ID)."""

    account_id: NonEmpty
    bank_txn_id: NonEmpty
    existing_txn_uid: TxnUid
    payload_id: NonEmpty
    existing_value_date: date
    new_value_date: date
    existing_amount_paise: int
    new_amount_paise: int


class ErasureCompleted(Payload):
    consent_id: NonEmpty
    keys_destroyed: Annotated[int, Field(ge=0)]


PAYLOADS: dict[EventKind, type[Payload]] = {
    EventKind.CONSENT_CHANGED: ConsentChanged,
    EventKind.PAYLOAD_FETCHED: PayloadFetched,
    EventKind.FETCH_FAILED: FetchFailed,
    EventKind.NEW_CREDIT: NewCredit,
    EventKind.NEW_INVOICE: NewInvoice,
    EventKind.CORRECTION: Correction,
    EventKind.OVERRIDE: Override,
    EventKind.DQ_ALERT: DqAlert,
    EventKind.ACCEPT_GAP: AcceptGap,
    EventKind.TXN_ID_CONFLICT: TxnIdConflict,
    EventKind.ERASURE_COMPLETED: ErasureCompleted,
}


def parse_payload(kind: EventKind | str, payload_json: str) -> Payload:
    return PAYLOADS[EventKind(kind)].model_validate_json(payload_json)
