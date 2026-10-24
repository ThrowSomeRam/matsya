"""Berka outside check (benchmark only, eval/data/SOURCES.md).

    uv run --project intelligence --extra bench python eval/models/berka_checks.py

For each of the 682 loans, the account's contract tables hold only transactions value-dated
strictly before loan.date (the leakage rule: with later data, min(balance) >= 0 separates
good and bad loans perfectly). The simulation engine runs at as_of = loan.date - 1 with the
obligations detector and the unscheduled-flow bootstrap (the flow forecaster is not built
yet; no invoices or sales on a personal account), 10,000 paths. Its P(shortfall by day 90)
is the score for a bad loan (status B or D).

Scores compared by AUC with a stratified bootstrap 95% interval (2,000 resamples), and the
paired difference against the best balance-only baseline:
- p_short_90 at threshold 0 (the engine's default) and at threshold = one monthly loan
  payment (known at origination): can the account cover an instalment;
- balance-only baselines: minus the last balance, minus the 90-day mean and minimum, and a
  5-fold cross-validated logistic regression on balance features.
Lead time is not measurable: the dataset gives a loan's final status, not when it went bad.
"""

import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loaders  # noqa: E402
import results  # noqa: E402

N_PATHS = 10_000
N_BOOT = 2_000
SEED = 20261025


def _one(loan: dict) -> dict:
    import os

    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    from matsya_intelligence.engine import build_learned_inputs, run_engine

    as_of = loan["date"] - timedelta(days=1)
    tables, rows = loaders.berka_tables(loan["account_id"], loan["date"])
    cats = loaders.berka_categories(rows)
    run_id = f"berka-outside:{loan['loan_id']}"
    out = {"loan_id": loan["loan_id"], "bad": loan["status"] in ("B", "D"), "n_txns": rows.height,
           "history_days": (as_of - rows["value_date"].min()).days if rows.height else 0}
    inp = build_learned_inputs(tables, cats, as_of, run_id, with_sales=False, sector_shocks=False)
    for name, thr in (("p_short_90_thr0", 0), ("p_short_90_thr_payment", int(round(loan["payments"] * 100)))):
        inp.threshold_paise = thr
        sf = run_engine(inp, run_id, N_PATHS).shortfall["p_short"].to_numpy()
        out[name] = float(sf[89])
        if thr == 0:
            out["p_short_30_thr0"], out["p_short_60_thr0"] = float(sf[29]), float(sf[59])
    bal = tables["ledger.balances"].sort("date")
    days = pl.DataFrame({"date": pl.date_range(bal["date"].min(), as_of, "1d", eager=True)})
    daily = days.join(bal.select("date", "closing_paise"), on="date", how="left").with_columns(
        pl.col("closing_paise").forward_fill())["closing_paise"].to_numpy() / 100
    last90 = daily[-90:]
    out |= {"last_balance": daily[-1], "mean90": last90.mean(), "min90": last90.min(), "std90": last90.std(),
            "mean_all": daily.mean(), "min_all": daily.min()}
    return out


def _auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, s))


def _boot(y, scores: dict[str, np.ndarray], rng) -> dict[str, np.ndarray]:
    pos, neg = np.flatnonzero(y), np.flatnonzero(~y)
    out = {k: np.empty(N_BOOT) for k in scores}
    for b in range(N_BOOT):
        i = np.concatenate([rng.choice(pos, pos.size), rng.choice(neg, neg.size)])
        for k, s in scores.items():
            out[k][b] = _auc(y[i], s[i])
    return out


def main() -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    import multiprocessing as mp

    loans = loaders.berka_raw()["loan"].to_dicts()
    with ProcessPoolExecutor(8, mp_context=mp.get_context("spawn")) as ex:
        df = pl.DataFrame(list(ex.map(_one, loans, chunksize=8))).sort("loan_id")
    y = df["bad"].to_numpy()
    feats = ["last_balance", "mean90", "min90", "std90", "mean_all", "min_all"]
    X = df.select(feats).to_numpy()
    lr = np.zeros(len(y))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)).fit(X[tr], y[tr])
        lr[te] = m.predict_proba(X[te])[:, 1]
    scores = {
        "engine_p_short_90_thr0": df["p_short_90_thr0"].to_numpy(),
        "engine_p_short_90_thr_payment": df["p_short_90_thr_payment"].to_numpy(),
        "engine_p_short_30_thr0": df["p_short_30_thr0"].to_numpy(),
        "balance_last_neg": -df["last_balance"].to_numpy(),
        "balance_mean90_neg": -df["mean90"].to_numpy(),
        "balance_min90_neg": -df["min90"].to_numpy(),
        "balance_logistic_cv5": lr,
    }
    point = {k: _auc(y, s) for k, s in scores.items()}
    boots = _boot(y, scores, np.random.default_rng(SEED))
    baselines = [k for k in scores if k.startswith("balance_")]
    best = max(baselines, key=point.get)
    n, n_bad = len(y), int(y.sum())
    rows, table = [], []
    for k in scores:
        lo, hi = np.quantile(boots[k], [0.025, 0.975])
        d = boots[k] - boots[best]
        dlo, dhi = np.quantile(d, [0.025, 0.975])
        bench = dict(benchmark="berka_outside_check", dataset="berka_pkdd99", component="simulation_engine",
                     model=k, baseline=best, horizon="90d" if "30" not in k else "30d")
        rows.append(results.row(metric="auc_bad_loan", value=point[k], n=n, ci=(lo, hi), note=f"bad {n_bad}", **bench))
        if k != best:
            rows.append(results.row(metric="auc_minus_best_balance_baseline", value=point[k] - point[best], n=n,
                                    ci=(dlo, dhi), note=f"paired bootstrap vs {best}", **bench))
        table.append({"score": k, "auc": point[k], "ci95": f"{lo:.3f}-{hi:.3f}",
                      f"minus_{best}": point[k] - point[best], "diff_ci95": f"{dlo:.3f}-{dhi:.3f}"})
    df.write_parquet(results.RESULTS / "berka_outside_scores.parquet")
    md = (f"# Berka outside check (benchmark only)\n\nn = {n} loans ({n_bad} bad: status B or D). Features only from "
          f"transactions before each loan date. Engine at {N_PATHS:,} paths; flows from the obligations detector and "
          f"the unscheduled bootstrap (no flow forecaster yet). Stratified bootstrap, {N_BOOT:,} resamples. Best "
          f"balance-only baseline: {best}. Median history before the loan: {int(df['history_days'].median())} days "
          f"(min {df['history_days'].min()}).\n\n" + results.md_table(pl.DataFrame(table)) +
          "\n\nLead time is not measurable: the dataset gives each loan's final status, not when it went bad.\n")
    results.write(rows, md, "berka_outside")
    print(md)


if __name__ == "__main__":
    main()
