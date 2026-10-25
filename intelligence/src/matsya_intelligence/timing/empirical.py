"""Per-buyer empirical timing: weekly hazard by week relative to the due week, shrunk to the buyer's
Beta-geometric chance.

Relative week j = k - d for an invoice paid in week k (since issue) with due week d, so j = 0 is
the week holding the due date. An invoice enters at j = 1 - d (its first week) and is at risk in
every week up to its payment week, or up to c - d if still open after c full weeks. Per bin:
    e_j = payments in bin j,  r_j = invoice-weeks at risk in bin j
The end bins J_LO and J_HI hold every week beyond them ("26 weeks or more"), so an invoice adds one
to r there for each week it spent in that range, and their hazard stays a weekly rate.
Open invoices count at risk with weight 1 - P(dead | open), from the Beta-geometric posterior, so
the never-paid share is left to that model.

Shrinkage target m_j: the buyer's fitted Beta-geometric chance, p_early before the due week
(j < 0) and p from it on (posterior means).
    h_j = (e_j + s m_j) / (r_j + s)   for j up to the last bin with r_j >= min_exposure
    h_j = m_j                          beyond it (no data: the parametric chance applies)
"""

import numpy as np

J_LO, J_HI = -26, 26
N_BINS = J_HI - J_LO + 1


def _bin(j) -> np.ndarray:
    return np.clip(np.asarray(j, dtype=int), J_LO, J_HI) - J_LO


def _exposure(lo: np.ndarray, hi: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted invoice-weeks at risk per bin for relative weeks lo..hi (inclusive) per invoice."""
    r = np.zeros(N_BINS)
    keep = hi >= lo
    lo, hi, w = lo[keep], hi[keep], w[keep]
    # end bins: every week at or beyond them
    r[0] += (w * np.maximum(np.minimum(hi, J_LO) - lo + 1, 0)).sum()
    r[-1] += (w * np.maximum(hi - np.maximum(lo, J_HI) + 1, 0)).sum()
    # interior bins: one week each, as a difference array
    a, b = np.maximum(lo, J_LO + 1), np.minimum(hi, J_HI - 1)
    inner = b >= a
    diff = np.zeros(N_BINS + 1)
    np.add.at(diff, a[inner] - J_LO, w[inner])
    np.add.at(diff, b[inner] - J_LO + 1, -w[inner])
    return r + np.cumsum(diff)[:N_BINS]


def counts(paid_weeks, paid_due, open_ages, open_due, open_alive=None) -> tuple[np.ndarray, np.ndarray]:
    """(e, r) per relative-week bin; r in invoice-weeks."""
    e = np.zeros(N_BINS)
    k, dk = np.asarray(paid_weeks, dtype=int), np.asarray(paid_due, dtype=int)
    c, dc = np.asarray(open_ages, dtype=int), np.asarray(open_due, dtype=int)
    w = np.ones(c.size) if open_alive is None else np.asarray(open_alive, dtype=float)
    np.add.at(e, _bin(k - dk), 1.0)
    has = c >= 1  # open with at least one full week survived
    r = _exposure(1 - dk, k - dk, np.ones(k.size)) + _exposure(1 - dc[has], c[has] - dc[has], w[has])
    return e, r


def parametric_chance(p_early: float, p: float) -> np.ndarray:
    """m_j per bin: p_early before the due week, p from it on."""
    return np.where(np.arange(J_LO, J_HI + 1) < 0, p_early, p)


def data_extent(r: np.ndarray, min_exposure: float) -> int:
    """Index of the last bin with adequate exposure (-1: none)."""
    adequate = np.flatnonzero(r >= min_exposure)
    return int(adequate[-1]) if adequate.size else -1


def buyer_hazard(e: np.ndarray, r: np.ndarray, m: np.ndarray, strength: float, min_exposure: float) -> np.ndarray:
    """Shrunk hazard up to the last bin with adequate exposure, the parametric chance beyond it."""
    h = (e + strength * m) / (r + strength)
    last = data_extent(r, min_exposure)
    h[last + 1:] = m[last + 1:]
    return h


def beyond_data_as_nan(h: np.ndarray, extent: int) -> np.ndarray:
    """The hazard for the simulation engine: NaN beyond the data, where the engine uses the per-draw
    Beta-geometric chance (keeps its parameter uncertainty) rather than the posterior mean in h."""
    out = h.astype(float).copy()
    out[extent + 1:] = np.nan
    return out


def predict_open_empirical(h: np.ndarray, q: float, age: int, horizon: int, due_week: int = 1
                           ) -> tuple[np.ndarray, float, float]:
    """Same outputs as model.predict_open: P(paid in week w from now), P(never), P(alive beyond)."""
    hz = h[_bin(age + np.arange(1, horizon + 1) - due_week)]
    survive_before = np.concatenate([[1.0], np.cumprod(1 - hz[:-1])])
    paid = (1 - q) * survive_before * hz
    return paid, float(q), float((1 - q) * np.prod(1 - hz))
