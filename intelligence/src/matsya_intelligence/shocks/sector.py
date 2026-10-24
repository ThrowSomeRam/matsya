"""Sector lateness shocks from a tenant's own payment history.

Residuals. Every invoice-week at risk (weeks 1..k for a paid invoice, 1..c for
an open one) has an expected payment chance h and an outcome y (1 in the week
it was paid). h is the buyer's posterior-mean weekly chance (p_early before the
due week, p after) times P(alive | unpaid after k - 1 weeks): without that
factor, never-paid invoices add expected payments that can never arrive and
the series reads as permanent lateness (it did: mean z about +0.2).

For a common logit shift delta in one calendar week, the score at delta = 0 is
sum(y - h) and the information sum h(1 - h), so a one-step estimate is

    delta_hat = sum(y - h) / sum h(1 - h),   Var = 1 / sum h(1 - h)

Lateness is z = -delta_hat (positive = slower than usual). Weeks with more
invoices at risk get smaller observation noise in the Kalman filter.

Model and forecast. Per sector, z follows an AR(1) (shocks.kalman), fitted by
grid MLE on the residual series; each simulated path starts from the filtered
posterior of z at the last full week and runs forward. The engine applies
shift = -z to logit p of every buyer in the sector (loading 1; per-buyer
loadings are not estimated yet).

Base logit. The posterior p is already the marginal weekly chance, averaged
over the shocks seen in the past. Applying the shock on top of logit p would
count them twice: for small p, E[sigmoid(logit p - z)] > p (Jensen), so
receipts would arrive early on average. Instead each p is replaced by a base
chance sigmoid(b), with b solved so that the shock-averaged chance equals p
under the sector's long-run (stationary) shock distribution z ~ N(0, s^2):

    E_z[sigmoid(b - z)] = p,   s^2 = q / (1 - rho^2)

solved by Newton on Gauss-Hermite quadrature. The filtered current z then
moves the chance away from p only by what is known now.

Caveat: h comes from posteriors fitted on the same data with no shock term,
so z measures deviations from each buyer's own average, as intended, but the
residuals are mildly shrunk toward zero.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.shocks.kalman import AR1Fit, fit_ar1


def lateness_residuals(labels: pl.DataFrame, means: dict[str, tuple[float, float, float]], sector_of: dict[str, str],
                       as_of: date) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """sector -> (week index j <= -1 relative to as_of, z_obs, R); NaN z_obs where no invoice was at risk."""
    acc: dict[tuple[str, int], list[float]] = {}
    for r in labels.iter_rows(named=True):
        pe, p, pi = means[r["buyer_id"]]
        paid = r["state"] == "paid"
        n = r["k"] if paid else r["c"]
        survive = 1.0  # P(unpaid after kk - 1 weeks | alive)
        for kk in range(1, (n or 0) + 1):
            j = (r["issue_date"] + timedelta(days=7 * (kk - 1)) - as_of).days // 7
            hz = pe if kk < r["d"] else p
            h = hz * (1 - pi / (pi + (1 - pi) * survive))
            survive *= 1 - hz
            y = 1.0 if paid and kk == r["k"] else 0.0
            a = acc.setdefault((sector_of[r["buyer_id"]], j), [0.0, 0.0, 0.0])
            a[0] += y
            a[1] += h
            a[2] += h * (1 - h)
    out = {}
    for sector in {s for s, _ in acc}:
        js = sorted(j for s, j in acc if s == sector)
        weeks = np.arange(js[0], max(js[-1], -1) + 1)
        z, R = np.full(weeks.size, np.nan), np.full(weeks.size, np.inf)
        for i, j in enumerate(weeks):
            a = acc.get((sector, j))
            if a and a[2] > 0:
                z[i], R[i] = -(a[0] - a[1]) / a[2], 1 / a[2]
        out[sector] = (weeks, z, R)
    return out


GH_X, GH_W = np.polynomial.hermite.hermgauss(32)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def shock_averaged(b: np.ndarray, var: float) -> np.ndarray:
    """E[sigmoid(b - z)] for z ~ N(0, var), elementwise in b."""
    z = np.sqrt(2 * var) * GH_X
    return (_sigmoid(np.asarray(b, dtype=float)[..., None] - z) * GH_W).sum(axis=-1) / np.sqrt(np.pi)


def base_chance(p: np.ndarray, var: float, iters: int = 50) -> np.ndarray:
    """sigmoid(b) with E[sigmoid(b - z)] = p, z ~ N(0, var). Identity when var = 0."""
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    if var <= 0:
        return p
    z = np.sqrt(2 * var) * GH_X
    b = np.log(p / (1 - p))
    for _ in range(iters):
        sg = _sigmoid(b[..., None] - z)
        f = (sg * GH_W).sum(axis=-1) / np.sqrt(np.pi) - p
        fp = (sg * (1 - sg) * GH_W).sum(axis=-1) / np.sqrt(np.pi)
        step = f / np.maximum(fp, 1e-300)
        b = b - step
        if np.abs(step).max() < 1e-12:
            break
    return _sigmoid(b)


@dataclass
class SectorShocks:
    fits: dict[str, AR1Fit]

    def stationary_var(self, sector: str) -> float:
        """Long-run variance of the sector's z; 0 for sectors without a fit (no shock applied)."""
        return self.fits[sector].stationary_var if sector in self.fits else 0.0

    def shift_sampler(self, buyer_sector: list[str]):
        """Engine hook: (n_paths, horizon, rng) -> shift(path, buyer, week_start_day) = -z."""
        sectors = sorted(self.fits)
        sidx = np.array([sectors.index(s) if s in self.fits else -1 for s in buyer_sector])

        def sampler(n_paths: int, horizon: int, rng: np.random.Generator):
            n_weeks = horizon // 7 + 2
            z = np.zeros((n_paths, len(sectors) + 1, n_weeks))  # last sector slot = no fit, z = 0
            for i, s in enumerate(sectors):
                z[:, i, :] = self.fits[s].simulate(n_paths, n_weeks, rng)

            def shift(path, buyer, week_start_day):
                j = np.clip((week_start_day - 1) // 7, 0, n_weeks - 1)
                out = -z[path, sidx[buyer], j]
                return np.where(week_start_day >= 1, out, 0.0)  # past weeks: no shift (survival as fitted)

            return shift

        return sampler


def fit_sector_shocks(labels: pl.DataFrame, means: dict[str, tuple[float, float, float]], sector_of: dict[str, str],
                      as_of: date) -> SectorShocks:
    res = lateness_residuals(labels, means, sector_of, as_of)
    return SectorShocks(fits={s: fit_ar1(z, R) for s, (_, z, R) in res.items() if np.isfinite(z).sum() >= 8})
