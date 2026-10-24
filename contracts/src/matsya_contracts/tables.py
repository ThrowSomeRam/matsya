"""One row model per table. Field order is column order.

Conventions:
- Money is integer paise (int64), never floats. Fields typed Paise or NonNegPaise.
- Dates are ISO dates; timestamps are UTC (tz-aware). IST is applied only at display.
- Every row carries tenant_id, except the global ref.* tables (ref.buyers,
  ref.buyer_disclosures), which hold public facts. There is no sentinel tenant.
- ledger.transactions.amount_paise is signed: + credit, - debit. direction is redundant
  and must agree. Zero-amount rows never reach this table (A4 quarantines them).
- As-of tables (ledger.balances, ledger.invoices, ledger.matches, labels.invoice_outcomes)
  are append-only and carry known_at in their key. An as-of read takes, per key without
  known_at, the latest row with known_at <= as_of.
- ledger.parsed has no known_at of its own: a parsed row inherits its transaction's known_at.
"""

import hashlib
from datetime import date, datetime, timedelta
from typing import Annotated, ClassVar, Literal

import polars as pl
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from matsya_contracts.enums import (
    ActionKind,
    AmountOwner,
    BalanceSource,
    BuyerIdBasis,
    BuyerType,
    CategorySource,
    ConsentStatus,
    Direction,
    DriverKind,
    EventKind,
    InvoiceStatus,
    MatchMethod,
    ObligationKind,
    OwnerInputKind,
    PayerKind,
    QuarantineReason,
    Rail,
    SalesParam,
    Sector,
    SectorSource,
    SettlementKind,
    TdsBasis,
    TermsSource,
    TxnCategory,
    TxnSource,
)
from matsya_contracts.events import OVERRIDE_LIST, parse_payload
from matsya_contracts.ids import (
    BuyerId,
    FiscalYear,
    Gstin,
    Ifsc,
    InvoiceId,
    NonEmpty,
    Pan,
    Sha256Hex,
    TenantId,
    TxnUid,
    buyer_id,
    pan_of_gstin,
)


class Pl:
    """Annotated marker: the Polars dtype a field maps to, when the default is wrong."""

    def __init__(self, dtype: pl.DataType):
        self.dtype = dtype


class _Money:
    pass


MONEY = _Money()


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be tz-aware UTC")
    return v


Paise = Annotated[int, MONEY, Pl(pl.Int64())]
NonNegPaise = Annotated[int, Field(ge=0), MONEY, Pl(pl.Int64())]
Int32 = Annotated[int, Pl(pl.Int32())]
UInt64 = Annotated[int, Field(ge=0, lt=2**64), Pl(pl.UInt64())]
Prob = Annotated[float, Field(ge=0.0, le=1.0)]
Utc = Annotated[datetime, AfterValidator(_utc)]

Writer = Literal["A", "B"]


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table: ClassVar[str]
    writer: ClassVar[Writer]
    pk: ClassVar[tuple[str, ...]]


class TenantRow(Row):
    tenant_id: TenantId


# ---------------------------------------------------------------- ops (Track A)


class Consent(TenantRow):
    table = "ops.consents"
    writer = "A"
    pk = ("tenant_id", "consent_id")

    consent_id: NonEmpty
    fip_id: NonEmpty
    purpose: NonEmpty
    range_from: date
    range_to: date
    frequency: NonEmpty
    status: ConsentStatus
    expires_at: Utc


class Event(TenantRow):
    table = "ops.events"
    writer = "A"
    pk = ("event_id",)

    event_id: NonEmpty
    kind: EventKind
    payload_json: str
    at: Utc

    @model_validator(mode="after")
    def _payload(self):
        parse_payload(self.kind, self.payload_json)
        return self


class DqQuarantine(TenantRow):
    """Rows A4 refused to write to ledger.transactions. Each raises a dq_alert event."""

    table = "ops.dq_quarantine"
    writer = "A"
    pk = ("tenant_id", "quarantine_id")

    quarantine_id: NonEmpty
    payload_id: NonEmpty
    account_id: NonEmpty
    reason: QuarantineReason
    value_date: date | None
    amount_paise: Paise | None  # as the bank sent it, signed by the payload's own sign
    direction: Direction | None  # from the payload's type field
    narration_raw: str | None
    known_at: Utc

    @model_validator(mode="after")
    def _reason(self):
        if self.reason is QuarantineReason.ZERO_AMOUNT and self.amount_paise not in (0, None):
            raise ValueError("zero_amount with a non-zero amount")
        return self


class OwnerInput(TenantRow):
    """Raw log of owner-provided facts (YAML/CSV loader). Projected by Track A into
    ref.tenant_buyers, ledger.invoices.terms_source and labels.txn_corrections."""

    table = "ops.owner_inputs"
    writer = "A"
    pk = ("tenant_id", "input_id")

    input_id: NonEmpty
    kind: OwnerInputKind
    subject_id: NonEmpty  # buyer_id or txn_uid, depending on kind
    value_json: str
    source_file: NonEmpty
    entered_at: Utc


# ---------------------------------------------------------------- raw (Track A)


class AaPayload(TenantRow):
    table = "raw.aa_payloads"
    writer = "A"
    pk = ("tenant_id", "payload_id")

    payload_id: NonEmpty
    consent_id: NonEmpty
    fip_id: NonEmpty
    fetched_at: Utc
    object_uri: NonEmpty
    sha256: Sha256Hex


# ---------------------------------------------------------------- ledger (Track A)


class Transaction(TenantRow):
    table = "ledger.transactions"
    writer = "A"
    pk = ("tenant_id", "txn_uid")

    txn_uid: TxnUid
    account_id: NonEmpty
    value_date: date
    known_at: Utc
    amount_paise: Paise  # signed: + credit, - debit; never zero
    direction: Direction
    narration_raw: str | None
    source: TxnSource
    dq_score: Prob
    dup_group_id: str | None

    @model_validator(mode="after")
    def _sign(self):
        if self.amount_paise == 0:
            raise ValueError("amount_paise is zero")
        if (self.amount_paise > 0) != (self.direction is Direction.CREDIT):
            raise ValueError("amount_paise sign disagrees with direction")
        return self


class Balance(TenantRow):
    table = "ledger.balances"
    writer = "A"
    pk = ("tenant_id", "account_id", "date", "known_at")

    account_id: NonEmpty
    date: date
    closing_paise: Paise
    continuity_ok: bool | None
    balance_source: BalanceSource
    # bank_reported: fetched_at of the carrying payload.
    # reconstructed: the latest known_at among the transactions behind it.
    known_at: Utc


class Parsed(TenantRow):
    """Fields parsed from a transaction's narration. Inherits the transaction's known_at;
    as-of reads join to ledger.transactions."""

    table = "ledger.parsed"
    writer = "A"
    pk = ("tenant_id", "txn_uid")

    txn_uid: TxnUid
    rail: Rail
    counterparty_vpa: str | None
    counterparty_ifsc: Ifsc | None
    utr: str | None
    reference: str | None
    gstin: Gstin | None


class Invoice(TenantRow):
    table = "ledger.invoices"
    writer = "A"
    pk = ("tenant_id", "invoice_id", "known_at")

    invoice_id: InvoiceId
    irn: Sha256Hex | None  # e-invoice only; Tally/Zoho/CSV invoices have none
    buyer_id: BuyerId
    issue_date: date
    due_date: date
    terms_days: Int32 | None
    terms_source: TermsSource
    gross_paise: NonNegPaise
    gst_paise: NonNegPaise | None
    expected_tds_paise: NonNegPaise | None
    status: InvoiceStatus
    known_at: Utc  # append-only: a status change is a new row

    @model_validator(mode="after")
    def _dates(self):
        if self.due_date < self.issue_date:
            raise ValueError("due_date before issue_date")
        return self


class Match(TenantRow):
    table = "ledger.matches"
    writer = "A"
    pk = ("tenant_id", "txn_uid", "invoice_id", "known_at")

    txn_uid: TxnUid
    invoice_id: InvoiceId
    allocated_paise: NonNegPaise
    tds_inferred_paise: NonNegPaise
    confidence: Prob
    method: MatchMethod
    payer_kind: PayerKind
    tds_rate: Prob | None  # schema only; the rate set is A6 config
    tds_basis: TdsBasis | None
    known_at: Utc

    @model_validator(mode="after")
    def _tds(self):
        if self.tds_basis is not None and self.tds_rate is None:
            raise ValueError("tds_basis without tds_rate")
        return self


# ---------------------------------------------------------------- labels (Track A)


class InvoiceOutcome(TenantRow):
    """Timing label for one invoice as known at known_at.

    - first_paid_date: date of the first allocation.
    - paid_date: first date where allocated + inferred TDS reaches gross minus a config
      tolerance.
    - paid_fraction: min(settled / gross, 1).
    - settlement_kind: null while open.
    - censored_at is set iff paid_date is null or settlement_kind is treds_discount. If
      censored_at is set, the timing label is censored regardless of paid_date.
    """

    table = "labels.invoice_outcomes"
    writer = "A"
    pk = ("tenant_id", "invoice_id", "known_at")

    invoice_id: InvoiceId
    buyer_id: BuyerId
    issue_date: date
    first_paid_date: date | None
    paid_date: date | None
    paid_fraction: Prob
    settlement_kind: SettlementKind | None
    censored_at: date | None
    known_at: Utc

    @model_validator(mode="after")
    def _rules(self):
        if self.paid_date is not None:
            if self.first_paid_date is None or self.first_paid_date > self.paid_date:
                raise ValueError("paid_date needs an earlier or equal first_paid_date")
            if self.settlement_kind is None:
                raise ValueError("paid_date needs settlement_kind")
        elif self.settlement_kind is not None:
            raise ValueError("settlement_kind must be null while open")
        if self.first_paid_date is not None and self.first_paid_date < self.issue_date:
            raise ValueError("first_paid_date before issue_date")
        must_censor = self.paid_date is None or self.settlement_kind is SettlementKind.TREDS_DISCOUNT
        if must_censor != (self.censored_at is not None):
            raise ValueError("censored_at must be set iff paid_date is null or settlement is treds_discount")
        if self.censored_at is not None and self.censored_at > self.known_at.date():
            raise ValueError("censored_at after known_at")
        return self


def timing_state(outcome: InvoiceOutcome) -> Literal["censored", "paid"]:
    """The label the timing model sees. censored_at wins over paid_date."""
    return "censored" if outcome.censored_at is not None else "paid"


class TxnCorrection(TenantRow):
    """Owner corrections: facts that retrain the categoriser. Latest corrected_at wins."""

    table = "labels.txn_corrections"
    writer = "A"
    pk = ("tenant_id", "correction_id")

    correction_id: NonEmpty
    txn_uid: TxnUid
    category: TxnCategory
    input_id: NonEmpty  # the ops.owner_inputs row it came from
    corrected_at: Utc

    @model_validator(mode="after")
    def _not_abstain(self):
        if self.category is TxnCategory.ABSTAIN:
            raise ValueError("a correction cannot be abstain")
        return self


# ---------------------------------------------------------------- ref (Track A)


class Buyer(Row):
    """Global, public facts. With id_basis pan, buyer_id = buyer_id(pan). With id_basis
    local (no PAN), buyer_id = local_buyer_id(tenant, name or VPA): such buyers are
    tenant-scoped and never pool across tenants. Owner-sourced sector lives in
    ref.tenant_buyers, so sector_source is never owner here."""

    table = "ref.buyers"
    writer = "A"
    pk = ("buyer_id",)

    buyer_id: BuyerId
    id_basis: BuyerIdBasis
    pan: Pan | None
    # Primary GSTIN only. A national buyer has one GSTIN per state; ref.buyer_gstins is
    # deferred to after G2.
    gstin: Gstin | None
    name: NonEmpty
    type: BuyerType
    sector: Sector
    sector_source: SectorSource
    treds_mandated: bool | None  # VERIFY: CPSE or turnover above Rs 250 cr (S.O. 4845(E))

    @model_validator(mode="after")
    def _rules(self):
        if self.id_basis is BuyerIdBasis.PAN:
            if self.pan is None or self.buyer_id != buyer_id(self.pan):
                raise ValueError("id_basis pan needs buyer_id == buyer_id(pan)")
        elif self.pan is not None or self.gstin is not None:
            raise ValueError("id_basis local means no PAN, so no GSTIN either")
        if self.gstin is not None and pan_of_gstin(self.gstin) != self.pan:
            raise ValueError("gstin does not embed pan")
        if self.sector_source is SectorSource.OWNER:
            raise ValueError("owner sector belongs in ref.tenant_buyers")
        if (self.sector is Sector.UNKNOWN) != (self.sector_source is SectorSource.UNKNOWN):
            raise ValueError("sector unknown iff sector_source unknown")
        return self


class TenantBuyer(TenantRow):
    table = "ref.tenant_buyers"
    writer = "A"
    pk = ("tenant_id", "buyer_id")

    buyer_id: BuyerId
    display_name: str | None  # the tenant's own name for the buyer
    owner_sector: Sector | None
    written_agreement: bool | None
    treds_onboarded: bool | None  # owner input only


def effective_sector(buyer: Buyer, tenant_buyer: TenantBuyer | None) -> tuple[Sector, SectorSource]:
    """exchange > cin_nic > owner > unknown."""
    if buyer.sector_source is not SectorSource.UNKNOWN:
        return buyer.sector, buyer.sector_source
    if tenant_buyer is not None and tenant_buyer.owner_sector not in (None, Sector.UNKNOWN):
        return tenant_buyer.owner_sector, SectorSource.OWNER
    return Sector.UNKNOWN, SectorSource.UNKNOWN


class BuyerDisclosure(Row):
    """Schedule III MSME trade-payables ageing and MSMED interest, per buyer and FY.

    Null means the column is absent from the report; 0 means reported as zero.
    """

    table = "ref.buyer_disclosures"
    writer = "A"
    pk = ("buyer_id", "fiscal_year")

    buyer_id: BuyerId
    fiscal_year: FiscalYear
    msme_not_due: NonNegPaise | None
    msme_lt_1y: NonNegPaise
    msme_1_2y: NonNegPaise
    msme_2_3y: NonNegPaise
    msme_gt_3y: NonNegPaise
    msme_disputed: NonNegPaise
    interest_due_unpaid: NonNegPaise
    msme_unbilled: NonNegPaise | None
    msme_total_outstanding: NonNegPaise | None

    @model_validator(mode="after")
    def _fy(self):
        start, end = int(self.fiscal_year[:4]), int(self.fiscal_year[5:])
        if (start + 1) % 100 != end:
            raise ValueError("fiscal_year must be consecutive years, e.g. 2025-26")
        return self


# ---------------------------------------------------------------- features (Track B)


class TxnCategoryRow(TenantRow):
    table = "features.txn_category"
    writer = "B"
    pk = ("tenant_id", "txn_uid")

    txn_uid: TxnUid
    category: TxnCategory
    confidence: Prob
    source: CategorySource
    needs_llm: bool  # abstained and waiting for the LLM


class Obligation(TenantRow):
    table = "features.obligations"
    writer = "B"
    pk = ("tenant_id", "obligation_id")

    obligation_id: NonEmpty
    kind: ObligationKind
    due_date: date
    amount_paise: NonNegPaise | None  # positive outflow; null when the sales forecaster owns it
    confidence: Prob
    amount_owner: AmountOwner

    @model_validator(mode="after")
    def _owner(self):
        if self.amount_paise is None and self.amount_owner is not AmountOwner.SALES_FORECASTER:
            raise ValueError("amount_paise null without the sales forecaster owning it")
        return self


class BuyerPrior(TenantRow):
    table = "features.buyer_priors"
    writer = "B"
    pk = ("tenant_id", "buyer_id", "as_of")

    buyer_id: BuyerId
    alpha: Annotated[float, Field(gt=0)]
    beta: Annotated[float, Field(gt=0)]
    cure_alpha: Annotated[float, Field(gt=0)]
    cure_beta: Annotated[float, Field(gt=0)]
    # Free string until B3 exists. Closing it to an enum later is breaking: version bump.
    source: NonEmpty
    as_of: date


class InvoiceTiming(TenantRow):
    table = "features.invoice_timing"
    writer = "B"
    pk = ("tenant_id", "invoice_id", "week")

    invoice_id: InvoiceId
    week: Annotated[int, Field(ge=1), Pl(pl.Int32())]  # weeks from as_of
    p_paid: Prob
    p_never: Prob  # same on every row of the invoice
    p_beyond: Prob  # alive but unpaid past the horizon
    past_cutoff: bool


def _monotone(*qs: int | float) -> bool:
    return all(a <= b for a, b in zip(qs, qs[1:]))


class FlowQuantile(TenantRow):
    table = "features.flow_quantiles"
    writer = "B"
    pk = ("tenant_id", "category", "date")

    category: TxnCategory
    date: date
    q05: Paise
    q25: Paise
    q50: Paise
    q75: Paise
    q95: Paise

    @model_validator(mode="after")
    def _order(self):
        if not _monotone(self.q05, self.q25, self.q50, self.q75, self.q95):
            raise ValueError("quantiles not monotone")
        return self


class SalesProcess(TenantRow):
    """Long format: one row per parameter. key is buyer_id for buyer_share, else null."""

    table = "features.sales_process"
    writer = "B"
    pk = ("tenant_id", "as_of", "name", "key")

    as_of: date
    name: SalesParam
    key: str | None
    value: float

    @model_validator(mode="after")
    def _key(self):
        if (self.name is SalesParam.BUYER_SHARE) != (self.key is not None):
            raise ValueError("key is set iff name is buyer_share")
        return self


# ---------------------------------------------------------------- forecast (Track B)


class Run(TenantRow):
    table = "forecast.runs"
    writer = "B"
    pk = ("run_id",)

    run_id: NonEmpty
    as_of: date
    input_snapshot_ids: list[str]
    model_versions_json: str
    seed: UInt64
    seed_streams_json: str
    scenario_id: str | None
    threshold_paise: Paise
    overrides_json: str  # JSON list of events.OverrideItem, for exact replay
    overrides_hash: Sha256Hex  # sha256 of overrides_json as stored (UTF-8)
    n_paths: Annotated[int, Field(ge=1)]
    horizon_days: Annotated[int, Field(ge=1), Pl(pl.Int32())]
    runway_days: Annotated[int | None, Pl(pl.Int32())]

    @model_validator(mode="after")
    def _overrides(self):
        OVERRIDE_LIST.validate_json(self.overrides_json)
        if hashlib.sha256(self.overrides_json.encode()).hexdigest() != self.overrides_hash:
            raise ValueError("overrides_hash does not match overrides_json")
        return self


class BalanceQuantile(TenantRow):
    table = "forecast.balance_quantiles"
    writer = "B"
    pk = ("run_id", "date")

    run_id: NonEmpty
    date: date
    q05: Paise
    q25: Paise
    q50: Paise
    q75: Paise
    q95: Paise

    @model_validator(mode="after")
    def _order(self):
        if not _monotone(self.q05, self.q25, self.q50, self.q75, self.q95):
            raise ValueError("quantiles not monotone")
        return self


class Shortfall(TenantRow):
    """cond: average depth over paths that go short by date. uncond: p_short x cond."""

    table = "forecast.shortfall"
    writer = "B"
    pk = ("run_id", "date")

    run_id: NonEmpty
    date: date
    day: Annotated[int, Field(ge=1), Pl(pl.Int32())]
    p_short: Prob
    expected_shortfall_cond_paise: NonNegPaise
    expected_shortfall_uncond_paise: NonNegPaise

    @model_validator(mode="after")
    def _uncond(self):
        if abs(self.expected_shortfall_uncond_paise - self.p_short * self.expected_shortfall_cond_paise) > 1:
            raise ValueError("uncond is not p_short x cond (to 1 paisa)")
        return self


class Scenario(TenantRow):
    table = "forecast.scenarios"
    writer = "B"
    pk = ("tenant_id", "scenario_id")

    scenario_id: NonEmpty
    name: NonEmpty
    params_json: str
    breaking_magnitude: float | None


class CalibrationState(TenantRow):
    table = "forecast.calibration_state"
    writer = "B"
    pk = ("tenant_id", "horizon", "as_of")

    horizon: Annotated[int, Field(ge=1), Pl(pl.Int32())]
    as_of: date
    alpha_t: Prob
    miss_rate_28d: Prob


class ShockState(TenantRow):
    table = "forecast.shock_state"
    writer = "B"
    pk = ("tenant_id", "sector", "week_start")

    sector: Sector
    week_start: date
    z: float
    z_sd: Annotated[float, Field(ge=0)]


# ---------------------------------------------------------------- decisions (Track B)


class Plan(TenantRow):
    table = "decisions.plans"
    writer = "B"
    pk = ("run_id", "rank")

    run_id: NonEmpty
    rank: Annotated[int, Field(ge=1), Pl(pl.Int32())]
    action: ActionKind
    target_id: str | None
    cost_paise: NonNegPaise
    p_short_after: Prob


class Driver(TenantRow):
    table = "decisions.drivers"
    writer = "B"
    pk = ("run_id", "driver_kind", "driver_id")

    run_id: NonEmpty
    driver_kind: DriverKind
    driver_id: NonEmpty
    delta_p_short: Annotated[float, Field(ge=-1.0, le=1.0)]


MODELS: tuple[type[Row], ...] = (
    Consent, Event, DqQuarantine, OwnerInput, AaPayload,
    Transaction, Balance, Parsed, Invoice, Match,
    InvoiceOutcome, TxnCorrection,
    Buyer, TenantBuyer, BuyerDisclosure,
    TxnCategoryRow, Obligation, BuyerPrior, InvoiceTiming, FlowQuantile, SalesProcess,
    Run, BalanceQuantile, Shortfall, Scenario, CalibrationState, ShockState,
    Plan, Driver,
)
TABLES: dict[str, type[Row]] = {m.table: m for m in MODELS}
GLOBAL_TABLES = frozenset(t for t, m in TABLES.items() if "tenant_id" not in m.model_fields)
NAMESPACE_WRITERS = {"ops": "A", "raw": "A", "ledger": "A", "labels": "A", "ref": "A",
                     "features": "B", "forecast": "B", "decisions": "B"}
