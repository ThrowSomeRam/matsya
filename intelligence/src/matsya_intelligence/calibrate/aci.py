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

An online step-size variant (Gibbs & Candes 2022) was benchmarked and removed: it was worse
than plain ACI at every horizon (docs/track-b-decisions.md, calibration component).

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
PRODUCTION_HORIZONS = (1, 7, 14, 30)  # run.py calibrates these (calibration.adapt_max_horizon_days)
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


@dataclass
class Pending:
    made_on: date
    target: date
    level: float  # miscoverage level used for the band
    lo: float
    hi: float


@dataclass
class HorizonState:
    alpha_t: float
    pending: list[Pending] = field(default_factory=list)
    history: list[tuple[date, int]] = field(default_factory=list)  # (target date, err)


class Calibrator:
    """Per-tenant calibration state, carried from one forecast run to the next."""

    def __init__(self, method: str = "aci", gamma: float | None = None, target_coverage: float | None = None,
                 horizons: tuple[int, ...] = HORIZONS, adapt_max_horizon: int | None = None):
        c = constants()
        self.alpha = 1 - (target_coverage if target_coverage is not None else float(c["target_coverage"]["value"]))
        self.method = method
        self.horizons = tuple(horizons)
        self.adapt_max_horizon = int(adapt_max_horizon if adapt_max_horizon is not None
                                     else c["adapt_max_horizon_days"]["value"])
        if method == "aci":
            self.gamma = float(gamma if gamma is not None else c["aci_gamma"]["paper_value"])
        elif method != "off":
            raise ValueError(f"unknown method {method!r} (aci | off)")
        self.state = {h: self._fresh() for h in self.horizons}

    def _fresh(self) -> HorizonState:
        return HorizonState(alpha_t=self.alpha)

    def level(self, h: int) -> float:
        """Miscoverage level the next horizon-h band is read at."""
        if not self.adapts(h):
            return self.alpha
        return self.state[h].alpha_t

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
            st.pending.append(Pending(made_on, made_on + timedelta(days=h), lvl, lo, hi))
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
                    st.alpha_t += self.gamma * (self.alpha - err)
                st.history.append((p.target, err))
                scored.append((h, p.made_on, p.target, p.level, p.lo, p.hi, y, err))
            st.pending = keep
        return pl.DataFrame(scored, schema={"horizon": pl.Int32, "made_on": pl.Date, "target_date": pl.Date,
                                            "level": pl.Float64, "lo": pl.Float64, "hi": pl.Float64,
                                            "actual": pl.Float64, "miss": pl.Int8}, orient="row")

    # ------------------------------------------------------------ persistence between runs

    def to_json(self) -> dict:
        """JSON-safe state (floats round-trip exactly through json), for the next run."""
        return {"method": self.method, "alpha": self.alpha, "gamma": getattr(self, "gamma", None),
                "horizons": list(self.horizons), "adapt_max_horizon": self.adapt_max_horizon,
                "state": {str(h): {"alpha_t": st.alpha_t,
                                   "pending": [[p.made_on.isoformat(), p.target.isoformat(), p.level, p.lo, p.hi]
                                               for p in st.pending],
                                   "history": [[d.isoformat(), e] for d, e in st.history]}
                          for h, st in self.state.items()}}

    @classmethod
    def from_json(cls, d: dict) -> "Calibrator":
        cal = cls(method=d["method"], gamma=d["gamma"], target_coverage=1 - d["alpha"],
                  horizons=tuple(d["horizons"]), adapt_max_horizon=d["adapt_max_horizon"])
        cal.alpha = d["alpha"]
        for h, st in d["state"].items():
            cal.state[int(h)] = HorizonState(
                alpha_t=st["alpha_t"],
                pending=[Pending(date.fromisoformat(a), date.fromisoformat(b), lvl, lo, hi)
                         for a, b, lvl, lo, hi in st["pending"]],
                history=[(date.fromisoformat(t), int(e)) for t, e in st["history"]])
        return cal

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
