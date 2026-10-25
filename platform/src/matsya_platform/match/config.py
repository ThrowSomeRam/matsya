"""Matcher thresholds. Tuned by hand until real review-cost data exists (docs/matcher-design.md)."""

from dataclasses import dataclass

# VERIFY: TDS rates likely on MSME receipts (Income-tax Act 2025 s.393(1) table entries,
# formerly 194Q 0.1%, 194C 1%/2%, 194J 10%), and 0% below threshold.
TDS_RATES = (0.0, 0.001, 0.01, 0.02, 0.10)
# Taxable value when the invoice shows GST separately; the whole amount when it cannot (an
# advance, paid before any invoice). Both are tried; which one fits is evidence (basis_evidence).
TDS_BASES = ("taxable_value", "gross")
TREDS_PLATFORMS = ("RXIL", "M1XCHANGE", "INVOICEMART", "TREDS")
STRICTNESS = ("permissive", "balanced", "strict")
RECEIPT_CATEGORIES = ("customer_receipt",)


@dataclass(frozen=True)
class MatchConfig:
    window_days: int = 120  # candidate credits: value_date in [issue_date, issue_date + window_days]
    tolerance_paise: int = 200  # a fit within Rs 2 is exact (rounding, bank charges on the credit)
    tds_rates: tuple[float, ...] = TDS_RATES
    tds_bases: tuple[str, ...] = TDS_BASES
    bundle_cap: int = 4  # largest bundle tried; a correctness guard as much as a performance one
    bundle_max_open: int = 15  # bundles search the buyer's 15 open invoices nearest the credit
    max_fee_share: float = 0.05  # VERIFY: largest plausible TReDS discount fee as a share of net
    em_passes: int = 3
    habit_penalty_paise: int = 500  # added to a hypothesis that is not the buyer's learned habit
    treds_penalty_paise: int = 100  # a discount explanation costs a little more than an exact one
    bundle_penalty_paise: int = 300  # per extra invoice in a bundle
    partial_penalty_paise: int = 10_000_000  # partials are a last resort, never compared with fits
    min_margin_paise: int = 200  # best must beat the runner-up by more than this, or it is ambiguous
    full_margin_paise: int = 5_000  # margin at which confidence stops being discounted
    treds_platforms: tuple[str, ...] = TREDS_PLATFORMS
    # Can a link with no invoice reference (arithmetic only) settle an invoice?
    #   permissive: yes (the behaviour before these checks existed);
    #   balanced: only when none of the advance-shaped checks below fires, else review;
    #   strict: never; every arithmetic-only link goes to review.
    # Placeholder default pending a decision (see the sweep in docs/matcher-design.md).
    strictness: str = "permissive"
    advance_days_before_due: int = 10  # an arithmetic-only credit earlier than this before due looks like an advance
    round_steps_paise: tuple[int, ...] = (1_000_00, 5_000_00, 10_000_00)  # a multiple of Rs 1,000 / 5,000 / 10,000
    contradiction_check: bool = True  # a later referenced payment displaces an arithmetic-only link
    category_gate: bool = True  # skip credits the categoriser confidently calls something other than a receipt
    category_min_confidence: float = 0.7
    receipt_categories: tuple[str, ...] = RECEIPT_CATEGORIES
    risk_days_scale: int = 30  # review risk: a paid date moving 30 days doubles the weight
    # TDS basis as evidence. TDS is calculated on the taxable value, excluding GST, when the
    # GST component is indicated separately on the invoice. An advance is paid before any
    # invoice exists, so no GST is indicated separately and the deduction falls on the whole
    # amount. An arithmetic-only fit that works only on the gross basis, for a buyer whose
    # learned habit is taxable value, is advance-shaped and goes to review.
    basis_evidence: bool = True
    # The basis test has its own tolerance. The amount tolerance (Rs 2) absorbs rounding and
    # bank charges; the gap between the two bases is rate x GST, which at 0.1% on a small
    # invoice is under Rs 2, so the amount tolerance would swallow it. Rounding the two TDS
    # figures moves their gap by at most 1 paisa, so 5 paise sits well below the smallest
    # realistic base gap (0.1% of a GST amount of Rs 50) and well above rounding noise.
    basis_tolerance_paise: int = 5

    def __post_init__(self):
        if self.strictness not in STRICTNESS:
            raise ValueError(f"strictness must be one of {STRICTNESS}")

    @property
    def accept_cost(self) -> int:
        """Largest cost an accepted single or bundle explanation may carry."""
        return self.tolerance_paise + max(self.treds_penalty_paise, self.habit_penalty_paise)
