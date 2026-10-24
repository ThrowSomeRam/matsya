"""Sales forecaster: future invoices and their linked purchases.

Owns (docs/track-b-decisions.md) the dates and amounts of future invoices and
of supplier payments, and the amount of GST (see sales/gst.py).

Fit from history at as_of:
  - weekly invoice count ~ NegBin(mean m, dispersion k), Var = m + m^2 / k, by
    method of moments over complete weeks [as_of - 7(j+1), as_of - 7j);
    k is capped at K_MAX (effectively Poisson) when Var <= m;
  - buyer mix: invoice-count shares, add-one smoothed;
  - (amount, terms) pairs resampled from the buyer's own invoices if it has
    >= MIN_OWN_INVOICES, else from all invoices;
  - purchase lag L: the lag maximising the correlation of 7-day smoothed daily
    invoice amounts with 7-day smoothed daily supplier payments L days earlier;
  - purchase ratio: supplier payments in [h0, as_of - L] over invoices issued
    in [h0 + L, as_of], so both windows cover the same orders.
Weekly counts are sampled independently (demand autocorrelation not modelled yet).

Demand level (a simplification of the demand AR(1)): sample() can scale each
path's order rate by a persistent level L ~ lognormal with mean 1. level_sigma
is fitted from 8-week block means of the weekly counts: their variance beyond
what NegBin sampling noise alone gives, (m + m^2/k) / 8, as a share of m^2.
Because k is itself fitted from weekly variance, this double counts some
variation and the result is a rough, slightly conservative spread.

Parameter uncertainty (param_draws=True in sample): each path uses one of
N_BOOT bootstrap replicates of (m, k, purchase ratio). Weeks are resampled as
moving blocks of BOOT_BLOCK_WEEKS (so demand autocorrelation inflates the
spread of m as it should; an iid-week bootstrap understates it), months are
resampled for the ratio.

Demand AR(1) (demand="ar1" in sample): latent x_t on the log order rate,
x_t = rho x_{t-1} + e_t. From the count autocovariances,
    gamma_1 = m^2 s2 rho,  gamma_2 = m^2 s2 rho^2   (s2 = Var(e^x) ~ var(x))
so rho = gamma_2 / gamma_1 and s2 = gamma_1 / (rho m^2); k is refitted from
what is left, m^2 / k = Var - m - m^2 s2. The current x_T comes from a Kalman
filter on y_t = log((n_t + 0.5) / m) with noise 1/m + 1/k. With ~50 weeks the
autocovariances are noisy; when they are not positive, x is switched off.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_contracts.enums import SalesParam

from matsya_intelligence.io.types import SALES_PROCESS
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.shocks.kalman import AR1Fit, kalman_filter

MIN_OWN_INVOICES = 10
K_MAX = 1e4
MAX_LAG_DAYS = 30
N_BOOT = 200
BOOT_BLOCK_WEEKS = 4


@dataclass
class SalesModel:
    tenant_id: str
    as_of: date
    mean_per_week: float
    dispersion: float
    n_weeks: int
    buyer_ids: list[str]
    mix: np.ndarray
    pools: dict[str, tuple[np.ndarray, np.ndarray]]  # buyer_id -> (gross_paise, terms_days) to resample
    purchase_ratio: float
    purchase_lag_days: int
    tds_ratio: dict[str, float]  # buyer_id -> expected TDS / gross, median over history
    level_sigma: float
    boot_mean: np.ndarray  # (N_BOOT,) bootstrap replicates
    boot_var: np.ndarray
    boot_ratio: np.ndarray
    demand: AR1Fit | None  # None when the autocovariances do not support an AR(1)

    def features(self) -> pl.DataFrame:
        rows = [
            ("order_mean_per_week", None, self.mean_per_week),
            ("order_dispersion", None, self.dispersion),
            ("history_weeks", None, float(self.n_weeks)),
            ("purchase_ratio", None, self.purchase_ratio),
            ("purchase_lag_days", None, float(self.purchase_lag_days)),
            ("demand_level_sigma", None, self.level_sigma),
            ("demand_ar_rho", None, self.demand.rho if self.demand else 0.0),
            ("demand_ar_sd", None, float(np.sqrt(self.demand.stationary_var)) if self.demand else 0.0),
            ("order_mean_boot_sd", None, float(self.boot_mean.std())),
            ("purchase_ratio_boot_sd", None, float(self.boot_ratio.std())),
        ] + [("buyer_share", b, float(s)) for b, s in zip(self.buyer_ids, self.mix)]
        return pl.DataFrame(
            [(self.tenant_id, self.as_of, n, k, v) for n, k, v in rows], schema=SALES_PROCESS, orient="row")

    def contract_features(self) -> pl.DataFrame:
        """features.sales_process: only names in the contract's SalesParam enum. The AR(1) and
        bootstrap-spread rows wait for an additive enum change (requested from Track A)."""
        known = [m.value for m in SalesParam]
        return self.features().filter(pl.col("name").is_in(known))

    def demand_levels(self, n_paths: int, rng: np.random.Generator) -> np.ndarray:
        s = self.level_sigma
        return np.exp(s * rng.standard_normal(n_paths) - s * s / 2)

    def _dispersion(self, m: np.ndarray, v: np.ndarray, s2: float) -> np.ndarray:
        excess = v - m - m * m * s2
        return np.where(excess > 0, np.minimum(K_MAX, m * m / np.maximum(excess, 1e-12)), K_MAX)

    def sample(self, n_paths: int, horizon_days: int, rng: np.random.Generator,
               level: np.ndarray | None = None, param_draws: bool = False,
               demand: str = "none") -> tuple[pl.DataFrame, pl.DataFrame]:
        """Future invoices issued in (as_of, as_of + horizon] and purchases paid in that window, per path.

        Invoices are drawn L days past the horizon too, so purchases near its end
        are not missing; those extra invoices are dropped from the invoice table.
        Purchases for invoices whose purchase date is on or before as_of are
        already in history and are not returned.
        """
        L = self.purchase_lag_days
        n_weeks = -(-(horizon_days + L) // 7)
        if param_draws:
            j = rng.integers(0, N_BOOT, n_paths)
            m, v, ratio = self.boot_mean[j], self.boot_var[j], self.boot_ratio[j]
        else:
            m = np.full(n_paths, self.mean_per_week)
            v = np.full(n_paths, self.mean_per_week + self.mean_per_week**2 / self.dispersion)
            ratio = np.full(n_paths, self.purchase_ratio)
        if demand == "ar1" and self.demand is not None:
            s2 = self.demand.stationary_var
            mult = np.exp(self.demand.simulate(n_paths, n_weeks, rng) - s2 / 2)
            k = self._dispersion(m, v, s2)
        else:
            mult = np.ones((n_paths, n_weeks)) * (1.0 if level is None else level[:, None])
            k = self._dispersion(m, v, 0.0)
        lam = rng.gamma(k[:, None], (m / k)[:, None] * mult, size=(n_paths, n_weeks))
        counts = rng.poisson(lam)
        path = np.repeat(np.repeat(np.arange(n_paths), n_weeks), counts.ravel())
        week = np.repeat(np.tile(np.arange(n_weeks), n_paths), counts.ravel())
        day = week * 7 + rng.integers(0, 7, size=path.size) + 1  # days after as_of
        buyer = rng.choice(len(self.buyer_ids), size=path.size, p=self.mix)
        gross = np.empty(path.size, dtype=np.int64)
        terms = np.empty(path.size, dtype=np.int32)
        for i, b in enumerate(self.buyer_ids):
            idx = np.flatnonzero(buyer == i)
            amounts, t = self.pools[b]
            pick = rng.integers(0, amounts.size, size=idx.size)
            gross[idx], terms[idx] = amounts[pick], t[pick]
        as_of = np.datetime64(self.as_of, "D")
        issue = as_of + day.astype("timedelta64[D]")
        invoices = pl.DataFrame({
            "path": path.astype(np.int32),
            "issue_date": issue,
            "buyer_id": np.array(self.buyer_ids, dtype=object)[buyer],
            "gross_paise": gross,
            "terms_days": terms,
        }).filter(pl.col("issue_date") <= self.as_of + timedelta(days=horizon_days))
        p_day = day - L
        keep = (p_day >= 1) & (p_day <= horizon_days)
        purchases = pl.DataFrame({
            "path": path[keep].astype(np.int32),
            "value_date": as_of + p_day[keep].astype("timedelta64[D]"),
            "amount_paise": np.round(ratio[path[keep]] * gross[keep]).astype(np.int64),
        })
        return invoices, purchases


def _daily(df: pl.DataFrame, date_col: str, amount_col: str, start: date, end: date) -> np.ndarray:
    days = pl.DataFrame({"d": pl.date_range(start, end, "1d", eager=True)})
    s = df.group_by(pl.col(date_col).alias("d")).agg(pl.col(amount_col).sum().abs().alias("a"))
    return days.join(s, on="d", how="left").fill_null(0).sort("d")["a"].to_numpy().astype(float)


def _smooth(x: np.ndarray, w: int = 7) -> np.ndarray:
    return np.convolve(x, np.ones(w) / w, mode="same")


def fit_sales(invoices: pl.DataFrame, txns: pl.DataFrame, categories: pl.DataFrame, as_of: date) -> SalesModel:
    inv = invoices.filter(pl.col("issue_date") < as_of)
    h0 = inv["issue_date"].min()

    # Weekly counts over complete weeks [as_of - 7(j+1), as_of - 7j).
    n_weeks = (as_of - h0).days // 7
    j = ((pl.lit(as_of) - pl.col("issue_date")).dt.total_days() - 1) // 7
    counts = np.bincount(inv.select(j.alias("j")).filter(pl.col("j") < n_weeks)["j"].to_numpy(), minlength=n_weeks)
    m, v = float(counts.mean()), float(counts.var(ddof=1))
    k = K_MAX if v <= m else min(K_MAX, m * m / (v - m))

    buyer_ids = sorted(inv["buyer_id"].unique().to_list())
    n_by = inv.group_by("buyer_id").len()
    n_map = dict(zip(n_by["buyer_id"], n_by["len"]))
    mix = np.array([n_map[b] + 1 for b in buyer_ids], dtype=float)
    mix /= mix.sum()
    pooled = (inv["gross_paise"].to_numpy(), inv["terms_days"].to_numpy())
    pools = {}
    for b in buyer_ids:
        own = inv.filter(pl.col("buyer_id") == b)
        pools[b] = (own["gross_paise"].to_numpy(), own["terms_days"].to_numpy()) if own.height >= MIN_OWN_INVOICES else pooled

    purch = txns.filter((pl.col("amount_paise") < 0) & (pl.col("value_date") <= as_of)).join(
        categories.select("txn_uid", "category"), on="txn_uid").filter(pl.col("category") == "supplier_payment")
    end = as_of - timedelta(days=1)
    sales_d = _smooth(_daily(inv, "issue_date", "gross_paise", h0, end))
    purch_d = _smooth(_daily(purch, "value_date", "amount_paise", h0, end))
    corrs = []
    for L in range(MAX_LAG_DAYS + 1):
        a, b = sales_d[L:], purch_d[: purch_d.size - L]
        corrs.append(np.corrcoef(a, b)[0, 1] if a.std() and b.std() else -1.0)
    L = int(np.argmax(corrs))
    p_sum = purch.filter(pl.col("value_date").is_between(h0, as_of - timedelta(days=L)))["amount_paise"].sum()
    s_sum = inv.filter(pl.col("issue_date") >= h0 + timedelta(days=L))["gross_paise"].sum()
    ratio = float(abs(p_sum) / s_sum) if s_sum else 0.0

    tds = inv.group_by("buyer_id").agg((pl.col("expected_tds_paise") / pl.col("gross_paise")).median().alias("r"))
    blocks = counts[: counts.size // 8 * 8].reshape(-1, 8).mean(axis=1)
    excess = blocks.var(ddof=1) - (m + m * m / k) / 8 if blocks.size > 2 else 0.0
    level_sigma = float(np.sqrt(np.log1p(max(excess, 0.0) / (m * m)))) if m > 0 else 0.0

    tenant = invoices["tenant_id"][0]
    rb = rng_for(f"sales-fit:{tenant}:{as_of}", "bootstrap")  # deterministic given inputs
    wk = counts[_block_indices(rb, n_weeks, BOOT_BLOCK_WEEKS, N_BOOT)]
    boot_mean, boot_var = wk.mean(axis=1), wk.var(axis=1, ddof=1)
    p_mo = purch.filter(pl.col("value_date").is_between(h0, as_of - timedelta(days=L))).group_by(
        (pl.col("value_date") + pl.duration(days=L)).dt.truncate("1mo").alias("mo")).agg(pl.col("amount_paise").sum().abs().alias("p"))
    s_mo = inv.filter(pl.col("issue_date") >= h0 + timedelta(days=L)).group_by(
        pl.col("issue_date").dt.truncate("1mo").alias("mo")).agg(pl.col("gross_paise").sum().alias("s"))
    mo = s_mo.join(p_mo, on="mo", how="left").fill_null(0).sort("mo")  # group_by order is not stable
    pick = rb.integers(0, mo.height, (N_BOOT, mo.height))
    boot_ratio = mo["p"].to_numpy()[pick].sum(axis=1) / np.maximum(mo["s"].to_numpy()[pick].sum(axis=1), 1)

    return SalesModel(
        tenant_id=tenant, as_of=as_of, mean_per_week=m, dispersion=float(k), n_weeks=n_weeks,
        buyer_ids=buyer_ids, mix=mix, pools=pools, purchase_ratio=ratio, purchase_lag_days=L,
        tds_ratio=dict(zip(tds["buyer_id"], tds["r"])), level_sigma=level_sigma,
        boot_mean=boot_mean, boot_var=boot_var, boot_ratio=boot_ratio, demand=_fit_demand(counts, m, v, k),
    )


def _block_indices(rng: np.random.Generator, n: int, block: int, reps: int) -> np.ndarray:
    """(reps, n) moving-block bootstrap indices: ceil(n / block) blocks of consecutive weeks, trimmed to n."""
    block = max(1, min(block, n))
    n_blocks = -(-n // block)
    starts = rng.integers(0, n - block + 1, (reps, n_blocks))
    return (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n]


def _fit_demand(counts: np.ndarray, m: float, v: float, k: float) -> AR1Fit | None:
    """AR(1) on the log order rate from count autocovariances (see module docstring)."""
    c = counts - m
    g1, g2 = float(np.mean(c[1:] * c[:-1])), float(np.mean(c[2:] * c[:-2]))
    if g1 <= 0 or g2 <= 0 or m <= 0:
        return None
    rho = float(np.clip(g2 / g1, 0.05, 0.95))
    s2 = min(g1 / (rho * m * m), max(v - m, 1e-9) / (m * m))  # cannot exceed the overdispersion
    q = s2 * (1 - rho * rho)
    excess = v - m - m * m * s2
    k_ar = K_MAX if excess <= 0 else m * m / excess
    y = np.log((counts + 0.5) / m)
    mean_T, var_T, ll = kalman_filter(y, np.full(y.size, 1 / m + 1 / k_ar), rho, q)
    return AR1Fit(rho=rho, q=q, mean_T=mean_T, var_T=var_T, loglik=ll)
