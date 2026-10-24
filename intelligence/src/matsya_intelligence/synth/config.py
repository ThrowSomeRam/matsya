"""Knobs for the synthetic world. Defaults are the "borderline" preset."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class TypePrior:
    """Type-level hyperpriors buyers are drawn from."""

    p_ab: tuple[float, float]  # Beta on the weekly payment chance p
    pi_ab: tuple[float, float]  # Beta on the never-paid share pi
    share: float  # fraction of buyers of this type


DEFAULT_TYPE_PRIORS = {
    "cpse": TypePrior(p_ab=(6, 10), pi_ab=(1, 99), share=0.2),  # mean p 0.38, pi 0.01
    "listed": TypePrior(p_ab=(4, 12), pi_ab=(2, 98), share=0.4),  # mean p 0.25, pi 0.02
    "unlisted": TypePrior(p_ab=(3, 15), pi_ab=(4, 46), share=0.4),  # mean p 0.17, pi 0.08
}


@dataclass(frozen=True)
class BehaviourChange:
    """From `at` on, one buyer pays more slowly and defaults more."""

    buyer_index: int
    at: date
    p_multiplier: float = 0.5  # applied to p, then clipped to (0, 1)
    pi_add: float = 0.0  # added to pi for invoices issued on or after `at`


@dataclass(frozen=True)
class WorldConfig:
    tenant_id: str = "t_synth"
    account_id: str = "acc_synth"
    start: date = date(2025, 4, 1)
    as_of: datetime = datetime(2026, 3, 31, 18, 0, tzinfo=timezone.utc)
    future_days: int = 120  # simulated past as_of; kept only in truth
    # True balance at as_of; the opening balance is back-solved to hit it.
    # Default is about 1.5 months of fixed costs (salary, rent, EMI, fee, PF, ESI ~ Rs 19 lakh/month).
    as_of_balance_paise: int = 28_60_000_00
    # Short = balance below this. 0 for now; later -(overdraft limit) + owner cushion.
    shortfall_threshold_paise: int = 0

    # Sector shock: z_t = rho z_{t-1} + eps, eps ~ N(0, sigma), weekly.
    # z is lateness: positive z means slower payment, so loadings are negative
    # on logit p.
    sectors: tuple[str, ...] = ("textiles", "auto_components", "fmcg")
    shock_rho: float = 0.8
    shock_sigma: float = 0.3
    loading_mean: float = -1.0
    loading_sd: float = 0.3

    # Buyers
    n_buyers: int = 10
    type_priors: dict[str, TypePrior] = field(default_factory=lambda: dict(DEFAULT_TYPE_PRIORS))
    buyer_mix_concentration: float = 2.0  # Dirichlet concentration for fixed buyer shares

    # Orders: weekly invoice count ~ NegBin(mean_t, dispersion), business level.
    # mean_t = order_mean_per_week * exp(x_t - var(x)/2), so the planted mean is the mean.
    # Var(count) = mean + mean^2 / dispersion.
    order_mean_per_week: float = 8.0
    order_dispersion: float = 5.0
    # Demand shock x: AR(1) on log order rate, separate from the payment shock z.
    demand_rho: float = 0.8
    demand_sigma: float = 0.1
    # Correlation of demand innovations with the (standardised) mean sector
    # lateness innovation. Negative means demand falls when buyers pay later.
    demand_shock_corr: float = 0.0
    amount_median_paise: int = 1_50_000_00  # Rs 1.5 lakh
    amount_sigma: float = 0.8  # lognormal sigma
    terms_choices: tuple[int, ...] = (15, 30, 45)
    gst_rate: float = 0.18
    part_payment_prob: float = 0.10
    has_irn: bool = True  # supplier above the Rs 5 crore e-invoicing threshold

    # Purchases: each order is bought on its order date for purchase_ratio x
    # gross; the invoice is issued purchase_lag_days (+- jitter) later.
    purchase_ratio: float = 0.45
    purchase_lag_days: int = 10
    purchase_lag_jitter_days: int = 3

    # Fixed operating outflows (monthly, fixed day), independent of sales
    salary_paise: int = 12_00_000_00
    salary_day: int = 1
    rent_paise: int = 2_00_000_00
    rent_day: int = 5
    emi_paise: int = 3_00_000_00
    emi_day: int = 10
    professional_fee_paise: int = 25_000_00  # CA retainer, TDS 10% under 194J
    professional_fee_day: int = 3
    pf_rate: float = 0.12  # VERIFY: employer PF share of salary, simplified
    esi_rate: float = 0.0325  # VERIFY: employer ESI share, simplified

    # Unscheduled flows (Poisson per day, lognormal amounts)
    misc_credits_per_day: float = 0.2
    misc_credit_median_paise: int = 5_000_00
    misc_expenses_per_day: float = 0.5
    misc_expense_median_paise: int = 1_500_00
    cash_withdrawals_per_week: float = 0.5
    owner_transfers_per_month: float = 1.0

    # Narration templates: which variants (0 hyphenated, 1 slashed) and bank
    # codes this world uses. Restrict to hold templates out for the categoriser.
    narration_variants: tuple[int, ...] = (0, 1)
    narration_banks: tuple[str, ...] = ("HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB", "BARB", "CNRB")

    # Misspecification knobs, all off by default
    lateness: str = "geometric"  # "geometric" | "lognormal"
    lognormal_sigma: float = 0.6
    behaviour_change: BehaviourChange | None = None
    month_end_clustering: bool = False
    month_end_prob: float = 0.7

    @property
    def as_of_date(self) -> date:
        return self.as_of.date()

    @classmethod
    def preset(cls, name: str, **overrides) -> "WorldConfig":
        return replace(cls(), **PRESETS[name], **overrides)


# Calibrated on the oracle: P(balance below threshold by day 90), from 60
# resampled futures per fixed past, median over past seeds 0..6 (2026-10-24):
#   healthy     0.33 -> ~0.02 (5 pasts: 0.00-0.40)
#   borderline  0.45 -> 0.28  (pasts: 0.03, 0.20, 0.20, 0.28, 0.42, 0.72, 0.77)
#   distressed  0.66 -> 0.90  (pasts: 0.62-1.00)
# The spread across pasts is real (buyer mix, receivables open at as_of), so
# the bands hold for the median past, not for every past. Only the purchase
# ratio (gross margin) differs.
PRESETS: dict[str, dict] = {
    "healthy": {"purchase_ratio": 0.33},
    "borderline": {},
    "distressed": {"purchase_ratio": 0.66},
}
