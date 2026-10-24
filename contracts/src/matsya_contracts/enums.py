"""Every shared enum. Additive only: members are never renamed or removed.

Consumers must map a value they do not know to a fallback rather than fail, so a new
member can ship without a version bump.
"""

from enum import StrEnum


class TxnCategory(StrEnum):
    CUSTOMER_RECEIPT = "customer_receipt"
    SUPPLIER_PAYMENT = "supplier_payment"
    SALARY = "salary"
    RENT = "rent"
    EMI = "emi"
    PROFESSIONAL_FEE = "professional_fee"
    GST_PAYMENT = "gst_payment"
    TDS_PAYMENT = "tds_payment"
    PF_ESI = "pf_esi"
    BANK_CHARGES = "bank_charges"
    REFUND = "refund"
    OWNER_TRANSFER = "owner_transfer"
    CASH_WITHDRAWAL = "cash_withdrawal"
    MISC_INCOME = "misc_income"
    MISC_EXPENSE = "misc_expense"
    ABSTAIN = "abstain"  # the categoriser declined; never a valid owner correction


# The 15 real classes, in the order of intelligence/categories.py.
CATEGORIES: tuple[TxnCategory, ...] = tuple(c for c in TxnCategory if c is not TxnCategory.ABSTAIN)


class CategorySource(StrEnum):
    RULE = "rule"
    MODEL = "model"
    LLM = "llm"
    OWNER = "owner"


class Direction(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class TxnSource(StrEnum):
    AA = "aa"
    STATEMENT_CSV = "statement_csv"
    SYNTH = "synth"


class BalanceSource(StrEnum):
    BANK_REPORTED = "bank_reported"
    RECONSTRUCTED = "reconstructed"


class Rail(StrEnum):
    UPI = "upi"
    NEFT = "neft"
    RTGS = "rtgs"
    IMPS = "imps"
    CARD = "card"
    ATM = "atm"
    CHARGES = "charges"
    UNKNOWN = "unknown"


class InvoiceStatus(StrEnum):
    OPEN = "open"
    PART_PAID = "part_paid"
    PAID = "paid"


class TermsSource(StrEnum):
    WRITTEN_AGREEMENT = "written_agreement"  # owner input only
    DOCUMENT_TERMS = "document_terms"  # credit days stated on the invoice or export
    DEFAULT_15D = "default_15d"  # VERIFY: MSMED Act s.15 wording, 15 days from acceptance with no agreement
    UNKNOWN = "unknown"


class PayerKind(StrEnum):
    BUYER = "buyer"
    TREDS_FINANCIER = "treds_financier"
    OTHER_ENTITY = "other_entity"
    UNKNOWN = "unknown"


class SettlementKind(StrEnum):
    BUYER = "buyer"
    TREDS_DISCOUNT = "treds_discount"  # cash is real; the timing label is censored
    OTHER_ENTITY = "other_entity"
    UNKNOWN = "unknown"


class TdsBasis(StrEnum):
    # VERIFY: whether TDS is computed on taxable value or the GST-inclusive amount
    TAXABLE_VALUE = "taxable_value"  # GST-exclusive
    GROSS = "gross"  # GST-inclusive


class MatchMethod(StrEnum):
    REFERENCE = "reference"  # invoice number or IRN found in the narration
    AMOUNT_WINDOW = "amount_window"
    IP_ASSIGNMENT = "ip_assignment"
    OWNER = "owner"


class BuyerIdBasis(StrEnum):
    PAN = "pan"  # global: buyer_id = sha256(PAN)[:32]
    LOCAL = "local"  # no PAN: tenant-scoped, never pooled across tenants


class BuyerType(StrEnum):
    CPSE = "cpse"
    LISTED = "listed"
    UNLISTED = "unlisted"


class Sector(StrEnum):
    """Shock-grouping sectors. Proposed list; the NIC-to-sector mapping lands at G2.

    The first three match the synthetic generator's sectors.
    """

    TEXTILES = "textiles"
    AUTO_COMPONENTS = "auto_components"
    FMCG = "fmcg"
    PHARMA = "pharma"
    CHEMICALS = "chemicals"
    METALS = "metals"
    ENGINEERING = "engineering"
    ELECTRONICS = "electronics"
    CONSTRUCTION = "construction"
    ENERGY = "energy"
    IT_SERVICES = "it_services"
    LOGISTICS = "logistics"
    FOOD_PROCESSING = "food_processing"
    RETAIL_TRADE = "retail_trade"
    OTHER = "other"  # known, but outside this list
    UNKNOWN = "unknown"


class SectorSource(StrEnum):
    """Precedence: exchange > cin_nic > owner > unknown."""

    EXCHANGE = "exchange"
    CIN_NIC = "cin_nic"
    OWNER = "owner"
    UNKNOWN = "unknown"


class ConsentStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"
    EXPIRED = "expired"


class QuarantineReason(StrEnum):
    ZERO_AMOUNT = "zero_amount"
    SIGN_DIRECTION_MISMATCH = "sign_direction_mismatch"


class OwnerInputKind(StrEnum):
    BUYER_SECTOR = "buyer_sector"
    WRITTEN_AGREEMENT = "written_agreement"
    TREDS_ONBOARDED = "treds_onboarded"
    TXN_CATEGORY = "txn_category"


class ObligationKind(StrEnum):
    TDS = "tds"
    PF = "pf"
    ESI = "esi"
    GST = "gst"
    SALARY = "salary"
    RENT = "rent"
    EMI = "emi"
    PROFESSIONAL_FEE = "professional_fee"


class AmountOwner(StrEnum):
    OBLIGATIONS_DETECTOR = "obligations_detector"
    SALES_FORECASTER = "sales_forecaster"


class SalesParam(StrEnum):
    ORDER_MEAN_PER_WEEK = "order_mean_per_week"
    ORDER_DISPERSION = "order_dispersion"
    HISTORY_WEEKS = "history_weeks"
    PURCHASE_RATIO = "purchase_ratio"
    PURCHASE_LAG_DAYS = "purchase_lag_days"
    DEMAND_LEVEL_SIGMA = "demand_level_sigma"
    BUYER_SHARE = "buyer_share"  # the only per-buyer parameter; key = buyer_id


class OverrideKind(StrEnum):
    ONE_OFF = "one_off"


class ActionKind(StrEnum):
    CHASE = "chase"
    TREDS_DISCOUNT = "treds_discount"
    MSMED_INTEREST = "msmed_interest"
    OVERDRAFT = "overdraft"
    SUPPLIER_STRETCH = "supplier_stretch"


class DriverKind(StrEnum):
    INVOICE = "invoice"
    BUYER = "buyer"
    SECTOR_SHOCK = "sector_shock"


class EventKind(StrEnum):
    """ops.events kinds. Each has exactly one payload model in events.PAYLOADS."""

    CONSENT_CHANGED = "consent_changed"
    PAYLOAD_FETCHED = "payload_fetched"
    FETCH_FAILED = "fetch_failed"
    NEW_CREDIT = "new_credit"
    NEW_INVOICE = "new_invoice"
    CORRECTION = "correction"
    OVERRIDE = "override"
    DQ_ALERT = "dq_alert"
    ACCEPT_GAP = "accept_gap"
    TXN_ID_CONFLICT = "txn_id_conflict"
    ERASURE_COMPLETED = "erasure_completed"
