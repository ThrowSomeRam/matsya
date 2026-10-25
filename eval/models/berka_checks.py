"""Berka outside check (benchmark only, eval/data/SOURCES.md).

    uv run --project intelligence --extra bench python eval/models/berka_checks.py

For each of the 682 loans, the account's contract tables hold only transactions value-dated
strictly before loan.date (the leakage rule: with later data, min(balance) >= 0 separates
good and bad loans perfectly). The simulation engine runs at as_of = loan.date - 1 with the
obligations detector and, for unscheduled flows, both the flow forecaster (quantile regression
with balance mean reversion) and the earlier same-weekday bootstrap, on the same seeds (no
invoices or sales on a personal account), 10,000 paths. Its P(shortfall by day 90) is the
score for a bad loan (status B or D).

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
FLOW_MODELS = ("quantile", "bootstrap")  # unscheduled flows: flow forecaster, earlier placeholder
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
    for flows in FLOW_MODELS:
        inp = build_learned_inputs(tables, cats, as_of, run_id, with_sales=False, sector_shocks=False,
                                   flow_forecaster=flows)
        out[f"{flows}_mode"] = inp.model_versions["unscheduled"]
        for name, thr in (("p_short_90_thr0", 0), ("p_short_90_thr_payment", int(round(loan["payments"] * 100)))):
            inp.threshold_paise = thr
            sf = run_engine(inp, run_id, N_PATHS).shortfall["p_short"].to_numpy()
            out[f"{flows}_{name}"] = float(sf[89])
            if thr == 0:
                out[f"{flows}_p_short_30_thr0"], out[f"{flows}_p_short_60_thr0"] = float(sf[29]), float(sf[59])
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
    scores = {f"engine_{f}_{k}": df[f"{f}_{k}"].to_numpy() for f in FLOW_MODELS
              for k in ("p_short_90_thr0", "p_short_90_thr_payment", "p_short_30_thr0")}
    scores |= {
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
          f"transactions before each loan date. Engine at {N_PATHS:,} paths; fixed flows from the obligations detector; "
          f"unscheduled flows from the flow forecaster (engine_quantile_*, modes: "
          f"{dict(zip(*np.unique(df['quantile_mode'].to_numpy(), return_counts=True)))}) or the earlier same-weekday "
          f"bootstrap (engine_bootstrap_*), same seeds. Data: Kaggle copy of PKDD'99 (SOURCES.md), so numbers are "
          f"comparable only with runs on that copy. Stratified bootstrap, {N_BOOT:,} resamples. Best "
          f"balance-only baseline: {best}. Median history before the loan: {int(df['history_days'].median())} days "
          f"(min {df['history_days'].min()}).\n\n" + results.md_table(pl.DataFrame(table)) +
          "\n\nLead time is not measurable: the dataset gives each loan's final status, not when it went bad.\n")
    results.write(rows, md, "berka_outside")
    print(md)


if __name__ == "__main__":
    main()
