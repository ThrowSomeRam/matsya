"""Scalar AR(1) state-space model with a Kalman filter, used for sector shocks and demand.

    state:        z_t = rho z_{t-1} + eps_t,   eps_t ~ N(0, q)
    observation:  y_t = z_t + eta_t,           eta_t ~ N(0, R_t)   (R_t known; NaN y_t = no data)

The stationary start is z_0 ~ N(0, q / (1 - rho^2)). rho and q are fitted by
maximising the Kalman (prediction-error) log-likelihood over a grid, which is
crude but stable on the ~50 weekly points a tenant has.
"""

from dataclasses import dataclass

import numpy as np

RHO_GRID = (0.0, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95)
Q_GRID = tuple(np.geomspace(1e-4, 1.0, 25))


@dataclass(frozen=True)
class AR1Fit:
    rho: float
    q: float
    mean_T: float  # filtered state mean after the last observation
    var_T: float
    loglik: float

    @property
    def stationary_var(self) -> float:
        return self.q / (1 - self.rho**2)

    def simulate(self, n_paths: int, n_steps: int, rng: np.random.Generator) -> np.ndarray:
        """(n_paths, n_steps) future states z_{T+1..T+n}, starting from the filtered posterior at T."""
        z = self.mean_T + np.sqrt(self.var_T) * rng.standard_normal(n_paths)
        out = np.empty((n_paths, n_steps))
        for j in range(n_steps):
            z = self.rho * z + np.sqrt(self.q) * rng.standard_normal(n_paths)
            out[:, j] = z
        return out


def kalman_filter(y: np.ndarray, R: np.ndarray, rho: float, q: float) -> tuple[float, float, float]:
    """(mean_T, var_T, log-likelihood)."""
    m, P = 0.0, q / (1 - rho**2) if rho < 1 else 1e6
    ll = 0.0
    for t in range(len(y)):
        if t > 0:
            m, P = rho * m, rho * rho * P + q
        if np.isfinite(y[t]):
            S = P + R[t]
            v = y[t] - m
            ll += -0.5 * (np.log(2 * np.pi * S) + v * v / S)
            K = P / S
            m, P = m + K * v, (1 - K) * P
    return m, P, ll


def fit_ar1(y: np.ndarray, R: np.ndarray) -> AR1Fit:
    best = None
    for rho in RHO_GRID:
        for q in Q_GRID:
            m, P, ll = kalman_filter(y, R, rho, q)
            if best is None or ll > best.loglik:
                best = AR1Fit(rho=rho, q=float(q), mean_T=m, var_T=P, loglik=ll)
    return best
