"""Shrink thin buyers toward their sector+type group.

Stage 1: fit every buyer under its type default prior.
Stage 2: per group (sector, type) with >= 2 buyers, fit a Beta to the member
         buyers' posterior means by method of moments:
             m = mean, v = var,  strength s = m (1 - m) / v - 1
         s is capped at prior_strength (constants) so a few real invoices can
         always override the group, and floored at 2. Done for p and pi.
Stage 3: refit each buyer under its group prior (type default if no group).

Thin buyers then sit near the group mean; heavy buyers on their own data.
Known bias: posterior means are already shrunk, so v understates the true
spread and s comes out too large; the cap limits the damage.
"""

import numpy as np

from matsya_intelligence.timing.model import TimingPrior

# Placeholders until the buyer priors component supplies disclosure-based
# defaults. decide: replace with ref.buyer_priors type rows.
TYPE_DEFAULTS = {
    "cpse": TimingPrior(alpha=2.4, beta=5.6, a=0.08, b=7.92),  # p 0.30, pi 0.01, strength 8
    "listed": TimingPrior(alpha=2.0, beta=6.0, a=0.16, b=7.84),  # p 0.25, pi 0.02
    "unlisted": TimingPrior(alpha=1.6, beta=6.4, a=0.4, b=7.6),  # p 0.20, pi 0.05
}


def _mom(means: np.ndarray, cap: float, floor: float = 2.0) -> tuple[float, float]:
    m = float(np.clip(means.mean(), 1e-3, 1 - 1e-3))
    v = float(means.var(ddof=1))
    s = cap if v <= 0 else float(np.clip(m * (1 - m) / v - 1, floor, cap))
    return m * s, (1 - m) * s


def group_prior(p_means: np.ndarray, pi_means: np.ndarray, cap: float) -> TimingPrior:
    alpha, beta = _mom(np.asarray(p_means), cap)
    a, b = _mom(np.asarray(pi_means), cap)
    return TimingPrior(alpha=alpha, beta=beta, a=a, b=b)
