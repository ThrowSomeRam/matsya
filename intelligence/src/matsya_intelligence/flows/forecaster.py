"""Flow forecaster: unscheduled flows only (bank charges, refunds, misc income and expense, cash
withdrawals, owner transfers). Invoices, purchases, GST and fixed obligations have their own owners.

Per unscheduled category c, daily net flow y_t (Rs thousand) is modelled by linear quantile
regression at q in {0.05, 0.25, 0.5, 0.75, 0.95} on
    weekday (6 dummies), day-of-month band (5 dummies), month-end (last 3 days), Indian festival
    window (the festival day and the 7 days before; dates in constants, VERIFY),
    balance deviation: (B_{t-1} - usual_t) / spread, usual_t = median closing balance over the 180
    days before t (in the forecast: the 180 days to as_of, held fixed), spread = the interquartile
    range over the 180 days to as_of (floored at Rs 1 lakh), so the fitted coefficient pulls flows
    the way that brings the balance back towards its usual level. A shorter window chases the
    balance: during a recovery the balance sits above its lagging median while flows are still
    positive, and the fit gets the sign wrong.
Crossing quantiles are sorted per day. The 0 and 1 ends of the quantile function extend the
end slopes over the last 0.05.

Persistence: days are not independent. Each category's uniform comes from an AR(1) Gaussian
copula, z_t = rho z_{t-1} + sqrt(1 - rho^2) e_t, u_t = Phi(z_t); rho is the lag-1 correlation of
the normal scores of the history's PIT values, clipped to [0, 0.9]. Sparse flows put many days on
a repeated quantile value (zero); such a day's PIT is drawn uniformly within its tied range
(randomised PIT, fixed seed), since a fixed mid-point would make quiet days look persistent.

Under min_weeks of history the quantile regression is not fitted: Chronos-2 (zero-shot) gives
the five quantiles per category and day, without the balance term; if Chronos-2 is not installed
(optional extra `chronos`), the same-weekday bootstrap is used and the mode says so.

The engine calls sample(..., pre_balance) after every other flow, day by day, so each path's
own balance feeds the deviation term.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl
from scipy.stats import norm

from matsya_intelligence.synth.world import PROCESS_OF_CATEGORY, load_constants

QUANTILES = np.array([0.05, 0.25, 0.5, 0.75, 0.95])
UNSCHEDULED = [c for c, p in PROCESS_OF_CATEGORY.items() if p == "unscheduled"]
UNIT = 100_000  # Rs thousand in paise: the regression's scale
DOM_BANDS = (6, 11, 16, 21, 26)  # day-of-month band starts after the first (1-5)
MONTH_END_DAYS = 3
FESTIVAL_LEAD_DAYS = 7
USUAL_DAYS = 180  # long enough that "usual" does not chase a recovering balance
RHO_MAX = 0.9
SPREAD_FLOOR_PAISE = 1_00_000_00.0  # Rs 1 lakh, so a flat balance history does not blow up the deviation


def festival_dates() -> list[date]:
    return [date.fromisoformat(str(d)) for d in load_constants()["flows"]["festivals"]["dates"]]


def calendar_features(days: list[date], festivals: list[date]) -> np.ndarray:
    """(n_days, 13): weekday Mon-Sat, day-of-month bands 2-6, month-end, festival window."""
    fest = set()
    for f in festivals:
        fest.update(f - timedelta(days=k) for k in range(FESTIVAL_LEAD_DAYS + 1))
    rows = []
    for d in days:
        wd = [1.0 if d.weekday() == k else 0.0 for k in range(6)]
        dom = [1.0 if lo <= d.day < hi else 0.0 for lo, hi in zip(DOM_BANDS, DOM_BANDS[1:] + (32,))]
        last = ((d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
        rows.append(wd + dom + [1.0 if d.day > last - MONTH_END_DAYS else 0.0, 1.0 if d in fest else 0.0])
    return np.array(rows, dtype=float)


def _fit_quantiles(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """(5, n_features + 1) coefficients, intercept first. Constant columns are dropped (coefficient 0)."""
    from sklearn.linear_model import QuantileRegressor

    keep = X.std(axis=0) > 0
    out = np.zeros((len(QUANTILES), X.shape[1] + 1))
    for i, q in enumerate(QUANTILES):
        m = QuantileRegressor(quantile=float(q), alpha=0.0, solver="highs").fit(X[:, keep], y)
        out[i, 0] = m.intercept_
        out[i, 1:][keep] = m.coef_
    return out


def _predict(coef: np.ndarray, X: np.ndarray) -> np.ndarray:
    """(n, 5) sorted quantiles."""
    return np.sort(coef[:, 0][None, :] + X @ coef[:, 1:].T, axis=1)


def _inverse(qv: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Quantile function from 5 knots per row at uniforms u, linear with extended ends."""
    lo = qv[:, 0] - (qv[:, 1] - qv[:, 0]) * 0.05 / 0.20
    hi = qv[:, 4] + (qv[:, 4] - qv[:, 3]) * 0.05 / 0.20
    knots_p = np.concatenate([[0.0], QUANTILES, [1.0]])
    vals = np.column_stack([lo, qv, hi])
    k = np.clip(np.searchsorted(knots_p, u, side="right") - 1, 0, len(knots_p) - 2)
    p0, p1 = knots_p[k], knots_p[k + 1]
    rows = np.arange(len(u))
    v0, v1 = vals[rows, k], vals[rows, k + 1]
    return v0 + (v1 - v0) * (u - p0) / (p1 - p0)


def _pit(qv: np.ndarray, y: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """PIT of y under each row's quantile function; ties at repeated knot values draw within their range."""
    knots_p = np.concatenate([[0.0], QUANTILES, [1.0]])
    lo = qv[:, 0] - (qv[:, 1] - qv[:, 0]) * 0.25
    hi = qv[:, 4] + (qv[:, 4] - qv[:, 3]) * 0.25
    vals = np.column_stack([lo, qv, hi])
    out = np.empty(len(y))
    for i in range(len(y)):
        v = vals[i]
        eq = np.flatnonzero(np.isclose(v, y[i], rtol=0.0, atol=1e-9))
        if eq.size:  # on a knot, or on a flat stretch of the quantile function
            out[i] = rng.uniform(knots_p[eq[0]], knots_p[eq[-1]]) if eq[-1] > eq[0] else knots_p[eq[0]]
        elif y[i] < v[0]:
            out[i] = 0.0
        elif y[i] > v[-1]:
            out[i] = 1.0
        else:
            k = np.searchsorted(v, y[i]) - 1
            out[i] = knots_p[k] + (knots_p[k + 1] - knots_p[k]) * (y[i] - v[k]) / (v[k + 1] - v[k])
    return out


def _rho(u: np.ndarray) -> float:
    z = norm.ppf(np.clip(u, 0.001, 0.999))
    if len(z) < 3 or z[:-1].std() == 0 or z[1:].std() == 0:
        return 0.0
    return float(np.clip(np.corrcoef(z[:-1], z[1:])[0, 1], 0.0, RHO_MAX))


@dataclass
class FlowForecaster:
    as_of: date
    categories: list[str]
    mode: str  # "quantile" | "chronos" | "bootstrap"
    coef: dict[str, np.ndarray] = field(default_factory=dict)  # quantile mode: (5, 14) per category
    rho: dict[str, float] = field(default_factory=dict)
    usual_balance: float = 0.0  # paise
    balance_spread: float = 1.0  # paise
    festivals: list[date] = field(default_factory=list)
    fixed_quantiles: dict[str, np.ndarray] = field(default_factory=dict)  # chronos mode: (H, 5) per category
    bootstrap: object = None  # bootstrap mode: the same-weekday sampler
    balance_feedback: bool = True  # tells the engine to pass pre_balance

    def quantiles(self, days: list[date], prev_balance: np.ndarray | None = None) -> dict[str, np.ndarray]:
        """Per category (n_days, 5) quantiles in paise; prev_balance (n_days,) for the deviation term."""
        if self.mode == "chronos":
            return {c: q[: len(days)] * UNIT for c, q in self.fixed_quantiles.items()}
        X = calendar_features(days, self.festivals)
        dev = np.zeros(len(days)) if prev_balance is None else (prev_balance - self.usual_balance) / self.balance_spread
        X = np.column_stack([X, dev])
        return {c: _predict(self.coef[c], X) * UNIT for c in self.categories}

    def sample(self, n_paths: int, horizon: int, rng: np.random.Generator,
               pre_balance: np.ndarray | None = None) -> np.ndarray:
        """(n_paths, horizon + 1) int64 net unscheduled flow, column 0 unused. pre_balance: each path's
        balance from every other flow (opening included); unscheduled flows are added day by day."""
        if self.mode == "bootstrap":
            return self.bootstrap(n_paths, horizon, rng)
        out = np.zeros((n_paths, horizon + 1), dtype=np.int64)
        z = {c: rng.standard_normal(n_paths) for c in self.categories}
        X_cal = calendar_features([self.as_of + timedelta(days=d) for d in range(1, horizon + 1)], self.festivals)
        cum = np.zeros(n_paths, dtype=np.int64)
        for d in range(1, horizon + 1):
            prev = (pre_balance[:, d - 1] if pre_balance is not None else np.full(n_paths, self.usual_balance)) + cum
            day_total = np.zeros(n_paths)
            for c in self.categories:
                if d > 1:
                    r = self.rho.get(c, 0.0)
                    z[c] = r * z[c] + np.sqrt(1 - r * r) * rng.standard_normal(n_paths)
                if self.mode == "chronos":
                    qv = np.repeat(self.fixed_quantiles[c][d - 1][None, :], n_paths, axis=0)
                else:
                    X = np.column_stack([np.repeat(X_cal[d - 1][None, :], n_paths, axis=0),
                                         (prev - self.usual_balance) / self.balance_spread])
                    qv = _predict(self.coef[c], X)
                day_total += _inverse(qv, norm.cdf(z[c]))
            out[:, d] = np.round(day_total * UNIT).astype(np.int64)
            cum = cum + out[:, d]
        return out


def daily_history(txns: pl.DataFrame, categories: pl.DataFrame, as_of: date, opening_paise: int,
                  cats: list[str] | None = None) -> tuple[list[date], dict[str, np.ndarray], np.ndarray]:
    """Days from the first transaction to as_of; net flow per unscheduled category per day (paise);
    closing balance per day, back-solved from the as-of balance."""
    cats = cats or UNSCHEDULED
    t = txns.filter(pl.col("value_date") <= as_of).join(categories.select("txn_uid", "category"), on="txn_uid", how="left")
    start = t["value_date"].min()
    days = pl.date_range(start, as_of, "1d", eager=True)
    grid = pl.DataFrame({"value_date": days})
    per = {}
    for c in cats:
        g = t.filter(pl.col("category") == c).group_by("value_date").agg(pl.col("amount_paise").sum())
        per[c] = grid.join(g, on="value_date", how="left").fill_null(0)["amount_paise"].to_numpy().astype(float)
    total = grid.join(t.group_by("value_date").agg(pl.col("amount_paise").sum()), on="value_date",
                      how="left").fill_null(0)["amount_paise"].to_numpy().astype(float)
    closing = opening_paise - (total.sum() - np.cumsum(total))  # closing on as_of equals the opening input
    return list(days), per, closing


def fit_flow_forecaster(txns: pl.DataFrame, categories: pl.DataFrame, as_of: date, opening_paise: int,
                        horizon: int = 90, bootstrap=None, cats: list[str] | None = None,
                        min_weeks: int | None = None, use_chronos: bool = True) -> FlowForecaster:
    """Fit on the as-of history. bootstrap: the fallback sampler when history is short and Chronos-2
    is unavailable."""
    k = load_constants()["flows"]
    min_weeks = min_weeks if min_weeks is not None else int(k["chronos_fallback_below_weeks"]["value"])
    days, per, closing = daily_history(txns, categories, as_of, opening_paise, cats)
    cats = list(per)
    festivals = festival_dates()
    if len(days) < 7 * min_weeks:
        if use_chronos:
            try:
                from matsya_intelligence.flows.chronos import chronos_quantiles

                q = {c: chronos_quantiles(per[c] / UNIT, horizon, QUANTILES) for c in cats}
                return FlowForecaster(as_of, cats, "chronos", fixed_quantiles=q, festivals=festivals,
                                      balance_feedback=False)
            except ImportError:
                pass
        return FlowForecaster(as_of, cats, "bootstrap", bootstrap=bootstrap, balance_feedback=False)
    recent = closing[-USUAL_DAYS:]
    usual = float(np.median(recent))
    q75, q25 = np.percentile(recent, [75, 25])
    spread = max(float(q75 - q25), SPREAD_FLOOR_PAISE)
    # day t's deviation: the previous day's closing against the median of the 90 days before it
    prev = closing[:-1]
    usual_t = np.array([np.median(closing[max(0, i - USUAL_DAYS + 1): i + 1]) for i in range(len(prev))])
    X = np.column_stack([calendar_features(days[1:], festivals), (prev - usual_t) / spread])
    coef, rho = {}, {}
    pit_rng = np.random.default_rng(0)  # deterministic tie-breaking only
    for c in cats:
        y = per[c][1:] / UNIT
        coef[c] = _fit_quantiles(X, y)
        rho[c] = _rho(_pit(_predict(coef[c], X), y, pit_rng))
    return FlowForecaster(as_of, cats, "quantile", coef=coef, rho=rho, usual_balance=usual, balance_spread=spread,
                          festivals=festivals)
