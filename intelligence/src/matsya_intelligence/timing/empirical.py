"""Per-buyer empirical timing: weekly hazard by week relative to the due week, shrunk to the group.

Relative week j = k - d for an invoice paid in week k (since issue) with due week d, so j = 0 is
the week holding the due date. An invoice enters at j = 1 - d (its first week) and is at risk in
every bin up to its payment bin, or up to c - d if still open after c full weeks. Per bin:
    e_j = payments in bin j,  r_j = invoices at risk in bin j
Open invoices count at risk with weight 1 - P(dead | open), from the Beta-geometric posterior, so
the never-paid share is left to that model.

Group hazard (pooled over the group's buyers): g_j = (E_j + 0.5) / (R_j + 1).
Buyer hazard, Beta-Binomial shrinkage per bin:  h_j = (e_j + s g_j) / (r_j + s).
Bins outside [J_LO, J_HI] are clamped to the end bins.
"""

import numpy as np

J_LO, J_HI = -26, 26
N_BINS = J_HI - J_LO + 1


def _bin(j) -> np.ndarray:
    return np.clip(np.asarray(j, dtype=int), J_LO, J_HI) - J_LO


def counts(paid_weeks, paid_due, open_ages, open_due, open_alive=None) -> tuple[np.ndarray, np.ndarray]:
    """(e, r) per relative-week bin."""
    e, r = np.zeros(N_BINS), np.zeros(N_BINS)
    k, dk = np.asarray(paid_weeks, dtype=int), np.asarray(paid_due, dtype=int)
    c, dc = np.asarray(open_ages, dtype=int), np.asarray(open_due, dtype=int)
    w = np.ones(c.size) if open_alive is None else np.asarray(open_alive, dtype=float)
    np.add.at(e, _bin(k - dk), 1.0)
    # at risk from entry bin to last bin, as a difference array over bins
    diff = np.zeros(N_BINS + 1)
    np.add.at(diff, _bin(1 - dk), 1.0)
    np.add.at(diff, _bin(k - dk) + 1, -1.0)
    has = c >= 1  # open with at least one full week survived
    np.add.at(diff, _bin(1 - dc[has]), w[has])
    np.add.at(diff, _bin(c[has] - dc[has]) + 1, -w[has])
    r = np.cumsum(diff)[:N_BINS]
    return e, r


def group_hazard(e: np.ndarray, r: np.ndarray) -> np.ndarray:
    return (e + 0.5) / (r + 1.0)


def buyer_hazard(e: np.ndarray, r: np.ndarray, g: np.ndarray, strength: float) -> np.ndarray:
    return (e + strength * g) / (r + strength)


def predict_open_empirical(h: np.ndarray, q: float, age: int, horizon: int, due_week: int = 1
                           ) -> tuple[np.ndarray, float, float]:
    """Same outputs as model.predict_open: P(paid in week w from now), P(never), P(alive beyond)."""
    hz = h[_bin(age + np.arange(1, horizon + 1) - due_week)]
    survive_before = np.concatenate([[1.0], np.cumprod(1 - hz[:-1])])
    paid = (1 - q) * survive_before * hz
    return paid, float(q), float((1 - q) * np.prod(1 - hz))
