"""Flow forecaster on Berka accounts (benchmark only, eval/data/SOURCES.md).

    uv run --project intelligence --extra bench python eval/models/flows_benchmark.py [n_accounts] [workers]

Accounts: a seeded sample of Berka accounts with at least 365 days of history before the origin
and 90 days after it. Origin (as_of): 90 days before the account's last transaction. Target: the
account's unscheduled flows (categories via loaders.berka_categories; fixed obligations such as
loan instalments and standing orders are owned by the obligations detector and left out).

Forecasts of the daily net unscheduled flow, days 1-90, and of its cumulative sum at days 30 and 90:
- flow forecaster: quantile regression per category, AR(1) copula persistence and the balance
  mean-reversion term (1,000 sample paths; the balance it reacts to is the opening balance plus
  the account's realised non-unscheduled flows, so the comparison isolates the unscheduled model);
- same weekday last week: the last observed week repeated (a point forecast);
- bootstrap: the engine's previous placeholder, a same-weekday day from the last 8 weeks
  (1,000 sample paths).
Scores: CRPS (from samples: E|X - y| - E|X - X'| / 2; a point forecast's CRPS is its absolute
error), and mean pinball loss over q in {0.05, 0.25, 0.5, 0.75, 0.95} (a point forecast uses its
value at every q). In Rs (CZK amounts read as rupee-scaled numbers; only ratios matter here).
Paired bootstrap over accounts (2,000 resamples) for the differences.
"""

import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loaders  # noqa: E402
import results  # noqa: E402

H = 90
N_SAMPLES = 1000
N_BOOT = 2000
SEED = 20261025
QS = np.array([0.05, 0.25, 0.5, 0.75, 0.95])
UNSCHED = {"bank_charges", "refund", "misc_income", "cash_withdrawal", "owner_transfer", "misc_expense"}


def _crps(samples: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per column: samples (n, T), y (T,)."""
    s = np.sort(samples, axis=0)
    n = s.shape[0]
    term1 = np.abs(s - y[None, :]).mean(axis=0)
    w = (2 * np.arange(1, n + 1) - n - 1)[:, None]  # E|X - X'| from sorted samples
    term2 = (w * s).sum(axis=0) / (n * n)
    return term1 - term2


def _pinball(qv: np.ndarray, y: np.ndarray) -> np.ndarray:
    """qv (len(QS), T), y (T,): mean over quantiles per column."""
    d = y[None, :] - qv
    return np.maximum(QS[:, None] * d, (QS[:, None] - 1) * d).mean(axis=0)


def _one(account_id: int) -> dict | None:
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    from matsya_intelligence.engine.learned import bootstrap_unscheduled
    from matsya_intelligence.flows import fit_flow_forecaster

    tr = loaders.berka_raw()["trans"].filter(pl.col("account_id") == account_id)
    if tr.height == 0:
        return None
    first, last = tr["value_date"].min(), tr["value_date"].max()
    as_of = last - timedelta(days=H)
    if (as_of - first).days < 365:
        return None
    tables, rows = loaders.berka_tables(account_id, as_of + timedelta(days=1))
    cats = loaders.berka_categories(rows)
    txns = tables["ledger.transactions"]
    opening = int(tables["ledger.balances"].sort("date")["closing_paise"][-1])
    ff = fit_flow_forecaster(txns, cats, as_of, opening, H, use_chronos=False)
    boot = bootstrap_unscheduled(txns, cats, as_of, full=False)

    # realised future flows by day, split into unscheduled and the rest
    fut_tables, fut_rows = loaders.berka_tables(account_id, as_of + timedelta(days=H + 1))
    fut_cats = loaders.berka_categories(fut_rows)
    f = fut_tables["ledger.transactions"].filter(pl.col("value_date") > as_of).join(fut_cats, on="txn_uid")
    f = f.with_columns(((pl.col("value_date") - pl.lit(as_of)).dt.total_days()).alias("day"),
                       pl.col("category").is_in(list(UNSCHED)).alias("u"))
    y = np.zeros(H + 1)
    other = np.zeros(H + 1)
    for r in f.group_by("day", "u").agg(pl.col("amount_paise").sum()).iter_rows(named=True):
        (y if r["u"] else other)[r["day"]] += r["amount_paise"]
    pre = (opening + np.cumsum(other))[None, :].repeat(N_SAMPLES, axis=0)

    rng = np.random.default_rng(SEED + account_id)
    s_ff = ff.sample(N_SAMPLES, H, rng, pre_balance=pre)[:, 1:] / 100
    s_bt = boot(N_SAMPLES, H, np.random.default_rng(SEED + account_id))[:, 1:] / 100
    y = y[1:] / 100

    # same weekday last week: the 7 days before as_of, repeated
    hist = txns.join(cats, on="txn_uid").filter(pl.col("category").is_in(list(UNSCHED))
                                                & (pl.col("value_date") > as_of - timedelta(days=7)))
    last_week = np.zeros(7)
    for r in hist.group_by("value_date").agg(pl.col("amount_paise").sum()).iter_rows(named=True):
        last_week[(r["value_date"] - (as_of - timedelta(days=6))).days] += r["amount_paise"] / 100
    naive = np.array([last_week[(d - 1) % 7] for d in range(1, H + 1)])

    out = {"account_id": account_id, "history_days": (as_of - first).days, "mode": ff.mode}
    for name, s in (("flow_forecaster", s_ff), ("bootstrap_8w", s_bt)):
        out[f"{name}_crps_daily"] = float(_crps(s, y).mean())
        out[f"{name}_pinball_daily"] = float(_pinball(np.quantile(s, QS, axis=0), y).mean())
        for h in (30, 90):
            c = s[:, :h].sum(axis=1)[:, None]
            out[f"{name}_crps_cum{h}"] = float(_crps(c, np.array([y[:h].sum()]))[0])
            out[f"{name}_pinball_cum{h}"] = float(_pinball(np.quantile(c, QS, axis=0), np.array([y[:h].sum()]))[0])
    out["same_weekday_last_week_crps_daily"] = float(np.abs(naive - y).mean())
    out["same_weekday_last_week_pinball_daily"] = float(_pinball(np.repeat(naive[None, :], 5, 0), y).mean())
    for h in (30, 90):
        e = naive[:h].sum() - y[:h].sum()
        out[f"same_weekday_last_week_crps_cum{h}"] = float(abs(e))
        out[f"same_weekday_last_week_pinball_cum{h}"] = float(_pinball(np.full((5, 1), naive[:h].sum()),
                                                                        np.array([y[:h].sum()]))[0])
    return out


def main(n_accounts: int = 300, workers: int = 4) -> None:
    accounts = loaders.berka_raw()["trans"]["account_id"].unique().sort().to_numpy()
    pick = np.random.default_rng(SEED).choice(accounts, size=min(len(accounts), int(n_accounts * 1.3)), replace=False)
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        res = [r for r in ex.map(_one, sorted(int(a) for a in pick), chunksize=4) if r is not None][:n_accounts]
    df = pl.DataFrame(res)
    df.write_parquet(results.RESULTS / "flows_benchmark_scores.parquet")
    n = df.height
    models = ("flow_forecaster", "bootstrap_8w", "same_weekday_last_week")
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, n, (N_BOOT, n))
    rows, table = [], []
    for metric in ("crps_daily", "pinball_daily", "crps_cum30", "pinball_cum30", "crps_cum90", "pinball_cum90"):
        base = df[f"same_weekday_last_week_{metric}"].to_numpy()
        for m in models:
            v = df[f"{m}_{metric}"].to_numpy()
            d = v[idx].mean(axis=1) - base[idx].mean(axis=1)
            ratio = v.mean() / base.mean()
            table.append({"metric": metric, "model": m, "mean (Rs)": f"{v.mean():,.1f}", "vs same weekday": f"{ratio:.3f}",
                          "diff 95% CI (Rs)": "" if m == "same_weekday_last_week" else
                          f"{np.percentile(d, 2.5):,.1f} to {np.percentile(d, 97.5):,.1f}"})
            rows.append(results.row("flows_berka", "berka_pkdd99", "flow forecaster", m, metric, v.mean(), n,
                                    "same_weekday_last_week", horizon=metric.split("_")[-1],
                                    ci=(np.percentile(v[idx].mean(axis=1), 2.5), np.percentile(v[idx].mean(axis=1), 97.5))))
    md = (f"# Flow forecaster on Berka (benchmark only)\n\nn = {n} accounts (seeded sample with >= 365 days of history "
          f"before the origin and 90 days after it; origin = 90 days before the last transaction). Target: daily net "
          f"unscheduled flow (Berka codes mapped by loaders.berka_categories; loan instalments and standing orders are "
          f"fixed obligations, left out), days 1-90, and its cumulative sum at days 30 and 90. Flow forecaster and "
          f"bootstrap: {N_SAMPLES:,} sample paths; the forecaster's balance term sees the opening balance plus the "
          f"realised non-unscheduled flows. Same weekday last week is a point forecast (CRPS = absolute error). Lower is "
          f"better; ratio = model / same weekday last week; paired bootstrap over accounts ({N_BOOT:,} resamples). "
          f"Data: Kaggle copy of PKDD'99 (SOURCES.md).\n\n" + results.md_table(pl.DataFrame(table)) + "\n")
    results.write(rows, md, "flows_benchmark")
    print(md)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
