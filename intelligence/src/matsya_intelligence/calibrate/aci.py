"""Calibration: adaptive conformal inference (ACI) per tenant and per horizon.

The engine's band for horizon h (days after as_of) is read off its own path distribution:
with miscoverage level a, the band is the empirical [a/2, 1 - a/2] quantiles of the simulated
balance on day h. The target miscoverage is alpha = 1 - target_coverage (0.20).

ACI (Gibbs & Candes 2021), one level per horizon:

    alpha_{t+1} = alpha_t + gamma (alpha - err_t),  err_t = 1 if the realised balance fell
                                                    outside the band issued at t

A miss lowers alpha_t (wider band), a hit raises it (narrower). The stored alpha_t is not
clipped, as the coverage guarantee needs; the band reads the quantiles at clip(alpha_t, 0, 1),
so alpha_t <= 0 means the whole real line (always a hit) and alpha_t >= 1 an empty band.

Horizons adapted: 1 to `adapt_max_horizon_days` (30) only. Longer horizons keep the engine's
own band (level alpha, never updated) but are still issued and scored, so their miss rate is
reported. Reason: with delayed feedback a 60- or 90-day band learns of a miss two to three
months later, and consecutive targets overlap, so misses arrive in long runs about events that
are already over; ACI reacts to all of them. On the replay benchmark the engine's own 60/90-day
bands were already near 0.80 and every step size made them worse.

Delayed feedback. A horizon-h band issued on day t targets day t + h and is scored only when
that day's balance is known (update() is given the balances known at as_of). Until then it
waits in `pending`; nothing about it touches alpha. Updates are applied in target-date order.

Online step size (method="online", Gibbs & Candes 2022, DtACI). One expert per gamma in the
grid, each running ACI on its own alpha^k. After each outcome, with beta_t the largest level
whose band still covers the realised balance (beta = 2 min(F(y), 1 - F(y)), F the paths' CDF),
each expert pays the pinball loss l(beta, alpha^k) = alpha (beta - alpha^k) - min(0, beta - alpha^k);
weights w_k <- (1 - sigma) w_k exp(-eta l_k) / sum + sigma / K; the level used is
sum w_k alpha^k (the paper samples an expert; the weighted mean is deterministic).
eta and sigma follow the paper's formulas with T from the constants file.

No randomness anywhere, so replays are bit-identical.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.io.types import CALIBRATION_STATE
from matsya_intelligence.synth.world import load_constants

HORIZONS = (1, 7, 14, 30, 60, 90)
MISS_WINDOW_DAYS = 28


def constants() -> dict:
    return load_constants()["calibration"]


def read_band(paths: np.ndarray, level: float) -> tuple[float, float]:
    """Central band of miscoverage `level` from simulated values (any order)."""
    a = min(max(level, 0.0), 1.0)
    if a <= 0.0:
        return -math.inf, math.inf
    lo, hi = np.quantile(paths, [a / 2, 1 - a / 2])
    return float(lo), float(hi)


def realised_level(paths: np.ndarray, y: float) -> float:
    """beta: the largest miscoverage level whose central band still covers y."""
    return float(min(1.0, 2 * min(np.mean(paths <= y), np.mean(paths >= y))))


def pinball(alpha: float, beta: float, theta: np.ndarray) -> np.ndarray:
    d = beta - theta
    return alpha * d - np.minimum(0.0, d)


@dataclass
class Pending:
    made_on: date
    target: date
    level: float  # miscoverage level used for the band
    lo: float
    hi: float
    paths: np.ndarray  # simulated balances on the target day (for beta, online method)
    expert_levels: np.ndarray | None  # each expert's level when the band was issued


@dataclass
class HorizonState:
    alpha_t: float
    experts: np.ndarray | None = None  # online: alpha^k per expert
    weights: np.ndarray | None = None
    pending: list[Pending] = field(default_factory=list)
    history: list[tuple[date, int]] = field(default_factory=list)  # (target date, err)


class Calibrator:
    """Per-tenant calibration state, carried from one forecast run to the next."""

    def __init__(self, method: str = "aci", gamma: float | None = None, target_coverage: float | None = None,
                 horizons: tuple[int, ...] = HORIZONS, grid: tuple[float, ...] | None = None,
                 adapt_max_horizon: int | None = None):
        c = constants()
        self.alpha = 1 - (target_coverage if target_coverage is not None else float(c["target_coverage"]["value"]))
        self.method = method
        self.horizons = tuple(horizons)
        self.adapt_max_horizon = int(adapt_max_horizon if adapt_max_horizon is not None
                                     else c["adapt_max_horizon_days"]["value"])
        if method == "aci":
            self.gamma = float(gamma if gamma is not None else c["aci_gamma"]["paper_value"])
        elif method == "online":
            self.grid = np.array(grid if grid is not None else c["aci_gamma"]["grid"], dtype=float)
            T = float(c["aci_gamma"]["online"]["horizon_T"]["value"])
            K, a = len(self.grid), self.alpha
            self.sigma = 1 / (2 * T)
            self.eta = math.sqrt(3 / T) * math.sqrt((math.log(K * T) + 2) / ((1 - a) ** 2 * a ** 3))
        elif method != "off":
            raise ValueError(f"unknown method {method!r} (aci | online | off)")
        self.state = {h: self._fresh() for h in self.horizons}

    def _fresh(self) -> HorizonState:
        if self.method == "online":
            k = len(self.grid)
            return HorizonState(alpha_t=self.alpha, experts=np.full(k, self.alpha), weights=np.full(k, 1 / k))
        return HorizonState(alpha_t=self.alpha)

    def level(self, h: int) -> float:
        """Miscoverage level the next horizon-h band is read at."""
        if not self.adapts(h):
            return self.alpha
        st = self.state[h]
        if self.method == "online":
            return float(st.weights @ st.experts)
        return st.alpha_t

    def adapts(self, h: int) -> bool:
        return self.method != "off" and h <= self.adapt_max_horizon

    # ------------------------------------------------------------ issue and score bands

    def issue(self, made_on: date, balance_paths: np.ndarray) -> pl.DataFrame:
        """Bands for every horizon from one engine run (balance_paths: (n_paths, H + 1), column h =
        day h after made_on). Each becomes pending until its target day's balance is known."""
        rows = []
        for h in self.horizons:
            if h >= balance_paths.shape[1]:
                continue
            paths = np.asarray(balance_paths[:, h], dtype=float)
            lvl = self.level(h)
            lo, hi = read_band(paths, lvl)
            nlo, nhi = read_band(paths, self.alpha)
            st = self.state[h]
            st.pending.append(Pending(made_on, made_on + timedelta(days=h), lvl, lo, hi, paths,
                                      None if st.experts is None else st.experts.copy()))
            rows.append((h, made_on + timedelta(days=h), lvl, lo, hi, nlo, nhi))
        return pl.DataFrame(rows, schema={"horizon": pl.Int32, "target_date": pl.Date, "level": pl.Float64,
                                          "lo": pl.Float64, "hi": pl.Float64, "nominal_lo": pl.Float64,
                                          "nominal_hi": pl.Float64}, orient="row")

    def update(self, as_of: date, known: dict[date, float]) -> pl.DataFrame:
        """Score every pending band whose target day is <= as_of and whose balance is in `known`
        (balances known by as_of). Bands whose outcome is not known yet stay pending."""
        scored = []
        for h in self.horizons:
            st = self.state[h]
            keep = []
            for p in sorted(st.pending, key=lambda p: (p.target, p.made_on)):
                if p.target > as_of or p.target not in known:
                    keep.append(p)
                    continue
                y = float(known[p.target])
                err = int(y < p.lo or y > p.hi)
                if self.adapts(h):
                    self._step(st, p, y, err)
                st.history.append((p.target, err))
                scored.append((h, p.made_on, p.target, p.level, p.lo, p.hi, y, err))
            st.pending = keep
        return pl.DataFrame(scored, schema={"horizon": pl.Int32, "made_on": pl.Date, "target_date": pl.Date,
                                            "level": pl.Float64, "lo": pl.Float64, "hi": pl.Float64,
                                            "actual": pl.Float64, "miss": pl.Int8}, orient="row")

    def _step(self, st: HorizonState, p: Pending, y: float, err: int) -> None:
        if self.method == "aci":
            st.alpha_t += self.gamma * (self.alpha - err)
        elif self.method == "online":
            beta = realised_level(p.paths, y)
            loss = pinball(self.alpha, beta, p.expert_levels)
            w = st.weights * np.exp(-self.eta * loss)
            st.weights = (1 - self.sigma) * w / w.sum() + self.sigma / len(w)
            errs = (beta < p.expert_levels).astype(float)
            st.experts = st.experts + self.grid * (self.alpha - errs)
            st.alpha_t = float(st.weights @ st.experts)

    # ------------------------------------------------------------ contract output

    def state_table(self, tenant_id: str, as_of: date) -> pl.DataFrame:
        """forecast.calibration_state rows. alpha_t is clipped to [0, 1] (the contract's range);
        a horizon appears once it has an outcome scored in the last 28 days (miss_rate_28d)."""
        rows = []
        for h in self.horizons:
            recent = [e for d, e in self.state[h].history if as_of - timedelta(days=MISS_WINDOW_DAYS) < d <= as_of]
            if recent:
                rows.append((tenant_id, h, as_of, min(max(self.level(h), 0.0), 1.0), float(np.mean(recent))))
        return pl.DataFrame(rows, schema=CALIBRATION_STATE, orient="row")
