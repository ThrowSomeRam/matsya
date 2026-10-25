"""Shrink thin buyers toward their sector+type group.

Stage 1: fit every buyer under its type default prior.
Stage 2: per group (sector, type) with >= 2 buyers, fit a Beta to the member
         buyers' posterior means by method of moments (p_early, p and pi each):
             m = mean, v = var,  strength s = m (1 - m) / v - 1
         s is capped at prior_strength (constants) so a few real invoices can
         always override the group, and floored at 2. The p prior's alpha is
         floored at group_prior_min_alpha (constants, 1.5) at the same mean, so a
         low-mean group cannot put mass near p = 0, where E[1/p] blows up.
Stage 3: refit each buyer under its group prior (type default if no group).

Thin buyers then sit near the group mean; heavy buyers on their own data.
Known bias: posterior means are already shrunk, so v understates the true
spread and s comes out too large; the cap limits the damage.
"""

import numpy as np

from matsya_intelligence.synth.world import load_constants
from matsya_intelligence.timing.model import TimingPrior



def type_defaults() -> dict[str, TimingPrior]:
    """Type default priors from the constants file (payment_timing.type_default_priors, status
    placeholder until the buyer priors component supplies disclosure-based rows)."""
    rows = load_constants()["payment_timing"]["type_default_priors"]
    return {t: TimingPrior(**{k: float(v) for k, v in p.items()}) for t, p in rows.items() if t != "status"}


def _mom(means: np.ndarray, cap: float, floor: float = 2.0, min_alpha: float = 0.0) -> tuple[float, float]:
    m = float(np.clip(means.mean(), 1e-3, 1 - 1e-3))
    v = float(means.var(ddof=1))
    s = cap if v <= 0 else float(np.clip(m * (1 - m) / v - 1, floor, cap))
    s = max(s, min_alpha / m)
    return m * s, (1 - m) * s


def group_prior(p_means: np.ndarray, pi_means: np.ndarray, cap: float,
                p_early_means: np.ndarray | None = None) -> TimingPrior:
    """Group Beta priors for p, pi and (if given) p_early; p_early defaults to Beta(1, 1)."""
    min_alpha = float(load_constants()["payment_timing"]["group_prior_min_alpha"]["value"])
    alpha, beta = _mom(np.asarray(p_means), cap, min_alpha=min_alpha)
    a, b = _mom(np.asarray(pi_means), cap)
    alpha_e, beta_e = (1.0, 1.0) if p_early_means is None else _mom(np.asarray(p_early_means), cap)
    return TimingPrior(alpha=alpha, beta=beta, a=a, b=b, alpha_e=alpha_e, beta_e=beta_e)
