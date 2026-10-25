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
Weekly counts are sampled independently, at the fitted (m, k) and purchase ratio. Three ways of
spreading demand were tried and removed: a persistent per-path demand level (unused by default),
an AR(1) on the log order rate, and per-path bootstrap draws of (m, k, ratio). On the learned-mode
calibration guard (100 pasts) all four combinations of the last two passed; with neither, mean
|gap| was 0.148 against 0.142 with both, and the simplest passing variant was kept
(docs/track-b-decisions.md).

Scenario order rate (order_mult in sample, from the scenarios component): the base orders
are drawn exactly as without it, then thinned (k < 1, each order kept with chance k) or
topped up (k > 1, extra Poisson((k - 1) lambda) orders per week) with a separate generator,
so the base draws are common to baseline and scenario. Purchases are generated per order,
so they follow at the purchase ratio. When orders are thinned, supplies for orders already
placed are still paid for: the purchase cut lags the sales cut by the fitted lag L plus
purchase_sticky_days, so a dropped order's purchase is kept if it falls on day <= L + sticky.
Added orders (k > 1) get their purchases as usual (selling more needs the supplies).
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_contracts.enums import SalesParam

from matsya_intelligence.io.types import SALES_PROCESS

MIN_OWN_INVOICES = 10
K_MAX = 1e4
MAX_LAG_DAYS = 30


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

    def features(self) -> pl.DataFrame:
        rows = [
            ("order_mean_per_week", None, self.mean_per_week),
            ("order_dispersion", None, self.dispersion),
            ("history_weeks", None, float(self.n_weeks)),
            ("purchase_ratio", None, self.purchase_ratio),
            ("purchase_lag_days", None, float(self.purchase_lag_days)),
        ] + [("buyer_share", b, float(s)) for b, s in zip(self.buyer_ids, self.mix)]
        return pl.DataFrame(
            [(self.tenant_id, self.as_of, n, k, v) for n, k, v in rows], schema=SALES_PROCESS, orient="row")

    def contract_features(self) -> pl.DataFrame:
        """features.sales_process: only names in the contract's SalesParam enum."""
        known = [m.value for m in SalesParam]
        return self.features().filter(pl.col("name").is_in(known))

    def sample(self, n_paths: int, horizon_days: int, rng: np.random.Generator,
               order_mult: float = 1.0,
               extra_rng: np.random.Generator | None = None,
               purchase_sticky_days: int = 0) -> tuple[pl.DataFrame, pl.DataFrame]:
        """Future invoices issued in (as_of, as_of + horizon] and purchases paid in that window, per path.

        Invoices are drawn L days past the horizon too, so purchases near its end
        are not missing; those extra invoices are dropped from the invoice table.
        Purchases for invoices whose purchase date is on or before as_of are
        already in history and are not returned.
        """
        L = self.purchase_lag_days
        n_weeks = -(-(horizon_days + L) // 7)
        m, k, ratio = self.mean_per_week, self.dispersion, np.full(n_paths, self.purchase_ratio)
        lam = rng.gamma(k, m / k, size=(n_paths, n_weeks))
        path, day, buyer, gross, terms = self._orders(rng.poisson(lam), rng)
        sticky = None  # dropped orders whose purchases are still paid: (path, day, gross)
        if order_mult != 1.0:
            if extra_rng is None:
                raise ValueError("order_mult needs extra_rng (the scenario stream)")
            if order_mult < 1.0:
                keep = extra_rng.random(path.size) < order_mult
                held = ~keep & (day - L <= L + purchase_sticky_days)
                sticky = path[held], day[held], gross[held]
                path, day, buyer, gross, terms = path[keep], day[keep], buyer[keep], gross[keep], terms[keep]
            else:
                more = self._orders(extra_rng.poisson(lam * (order_mult - 1.0)), extra_rng)
                path, day, buyer, gross, terms = (np.concatenate([x, y]) for x, y in
                                                  zip((path, day, buyer, gross, terms), more))
        as_of = np.datetime64(self.as_of, "D")
        issue = as_of + day.astype("timedelta64[D]")
        invoices = pl.DataFrame({
            "path": path.astype(np.int32),
            "issue_date": issue,
            "buyer_id": np.array(self.buyer_ids, dtype=object)[buyer],
            "gross_paise": gross,
            "terms_days": terms,
        }).filter(pl.col("issue_date") <= self.as_of + timedelta(days=horizon_days))
        p_path, p_day, p_gross = (path, day - L, gross) if sticky is None else (
            np.concatenate([path, sticky[0]]), np.concatenate([day, sticky[1]]) - L, np.concatenate([gross, sticky[2]]))
        keep = (p_day >= 1) & (p_day <= horizon_days)
        purchases = pl.DataFrame({
            "path": p_path[keep].astype(np.int32),
            "value_date": as_of + p_day[keep].astype("timedelta64[D]"),
            "amount_paise": np.round(ratio[p_path[keep]] * p_gross[keep]).astype(np.int64),
        })
        return invoices, purchases

    def _orders(self, counts: np.ndarray, rng: np.random.Generator):
        """(path, day, buyer index, gross, terms) for weekly order counts (n_paths, n_weeks)."""
        n_paths, n_weeks = counts.shape
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
        return path, day, buyer, gross, terms


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

    tenant = invoices["tenant_id"][0]
    return SalesModel(
        tenant_id=tenant, as_of=as_of, mean_per_week=m, dispersion=float(k), n_weeks=n_weeks,
        buyer_ids=buyer_ids, mix=mix, pools=pools, purchase_ratio=ratio, purchase_lag_days=L,
        tds_ratio=dict(zip(tds["buyer_id"], tds["r"])),
    )

