"""Shrink thin buyers toward their sector+type group.

Stage 1: fit every buyer under its type default prior.
Stage 2: per group (sector, type) with >= 2 buyers, fit a Beta to the member
         buyers' posterior means by method of moments (p_early, p_due, p and pi each):
             m = mean, v = var,  strength s = m (1 - m) / v - 1
         s is capped at prior_strength (constants) so a few real invoices can
         always override the group, and floored at 2.
Stage 3: refit each buyer under its group prior (type default if no group).

Thin buyers then sit near the group mean; heavy buyers on their own data.
Known bias: posterior means are already shrunk, so v understates the true
spread and s comes out too large; the cap limits the damage.
"""

import numpy as np

from matsya_intelligence.timing.model import TimingPrior

# Placeholders until the buyer priors component supplies disclosure-based
# defaults. decide: replace with ref.buyer_priors type rows.
# p_early: 0.05 at strength 8 for all types.
TYPE_DEFAULTS = {
    "cpse": TimingPrior(alpha=2.4, beta=5.6, a=0.08, b=7.92, alpha_e=0.4, beta_e=7.6),  # p 0.30, pi 0.01
    "listed": TimingPrior(alpha=2.0, beta=6.0, a=0.16, b=7.84, alpha_e=0.4, beta_e=7.6),  # p 0.25, pi 0.02
    "unlisted": TimingPrior(alpha=1.6, beta=6.4, a=0.4, b=7.6, alpha_e=0.4, beta_e=7.6),  # p 0.20, pi 0.05
}


def _mom(means: np.ndarray, cap: float, floor: float = 2.0) -> tuple[float, float]:
    m = float(np.clip(means.mean(), 1e-3, 1 - 1e-3))
    v = float(means.var(ddof=1))
    s = cap if v <= 0 else float(np.clip(m * (1 - m) / v - 1, floor, cap))
    return m * s, (1 - m) * s


def group_prior(p_means: np.ndarray, pi_means: np.ndarray, cap: float,
                p_early_means: np.ndarray | None = None, p_due_means: np.ndarray | None = None) -> TimingPrior:
    """Group Beta priors for p, pi and (if given) p_early and p_due; p_early defaults to Beta(1, 1),
    p_due to p's prior."""
    alpha, beta = _mom(np.asarray(p_means), cap)
    a, b = _mom(np.asarray(pi_means), cap)
    alpha_e, beta_e = (1.0, 1.0) if p_early_means is None else _mom(np.asarray(p_early_means), cap)
    alpha_d, beta_d = (None, None) if p_due_means is None else _mom(np.asarray(p_due_means), cap)
    return TimingPrior(alpha=alpha, beta=beta, a=a, b=b, alpha_e=alpha_e, beta_e=beta_e, alpha_d=alpha_d,
                       beta_d=beta_d)
