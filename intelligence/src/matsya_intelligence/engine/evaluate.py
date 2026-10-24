"""Engine checks against the oracle (resampled true futures of one fixed past).

true_vs_oracle: true-parameter mode; differences must be within Monte Carlo error.
learned_vs_oracle: learned mode; gap per past and a calibration table of
    predicted p_short(90) against the oracle's, binned.
gap1: learned mode with and without future invoices from the sales forecaster;
    mean error of the median balance against the oracle median, by horizon.
Categories come from generator truth here (the categoriser scores ~0.99 on
these worlds), to keep the runs fast.
"""

import time
from dataclasses import replace

import numpy as np

from matsya_intelligence.engine.core import run_engine
from matsya_intelligence.engine.learned import build_learned_inputs
from matsya_intelligence.engine.oracle_inputs import build_true_inputs
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth import WorldConfig, generate_world, resample_futures

DAYS = (30, 60, 90)
CURATED_BORDERLINE = (0, 1, 2)  # borderline preset pasts whose oracle p_short(90) is 0.1-0.4 (see PRESETS)


def _oracle(cfg: WorldConfig, past_seed: int, n_futures: int):
    fut = resample_futures(cfg, past_seed, n_futures)
    o = fut.oracle_shortfall(horizon_days=90)["p_short"].to_numpy()
    return fut, o


def true_vs_oracle(seeds=CURATED_BORDERLINE, n_futures: int = 200, n_paths: int = 10_000) -> list[dict]:
    cfg = WorldConfig(part_payment_prob=0.0)
    rows = []
    for s in seeds:
        fut, o = _oracle(cfg, s, n_futures)
        run_id = f"true-{s}"
        res = run_engine(build_true_inputs(fut.truths[0], n_paths, rng_for(run_id, "oracle:shocks")), run_id, n_paths)
        e = res.shortfall["p_short"].to_numpy()
        for d in DAYS:
            se = np.sqrt(o[d - 1] * (1 - o[d - 1]) / n_futures + e[d - 1] * (1 - e[d - 1]) / n_paths)
            rows.append({"seed": s, "day": d, "engine": float(e[d - 1]), "oracle": float(o[d - 1]), "se": float(se)})
    return rows


def learned_vs_oracle(seeds, n_futures: int = 60, n_paths: int = 10_000) -> dict:
    cfg = WorldConfig()
    per_seed = []
    for s in seeds:
        fut, o = _oracle(cfg, s, n_futures)
        obs, truth = fut.observed, fut.truths[0]
        cats = truth.transactions.select("txn_uid", "category")
        res = run_engine(build_learned_inputs(obs, cats, cfg.as_of_date, f"learned-{s}"), f"learned-{s}", n_paths)
        e = res.shortfall["p_short"].to_numpy()
        per_seed.append({"seed": s, **{f"engine_{d}": float(e[d - 1]) for d in DAYS},
                         **{f"oracle_{d}": float(o[d - 1]) for d in DAYS}})
    pred = np.array([r["engine_90"] for r in per_seed])
    true = np.array([r["oracle_90"] for r in per_seed])
    bins = [0, 0.05, 0.2, 0.4, 0.6, 0.8, 1.0001]
    table = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (pred >= lo) & (pred < hi)
        if m.any():
            table.append({"bin": f"[{lo:.2f}, {min(hi, 1):.2f})", "n": int(m.sum()),
                          "mean_predicted": float(pred[m].mean()), "mean_oracle": float(true[m].mean())})
    return {"per_seed": per_seed, "calibration_90": table, "mean_abs_gap_90": float(np.abs(pred - true).mean()),
            "mean_gap_90": float((pred - true).mean()), "n_futures": n_futures}


def gap1(seeds, n_futures: int = 60, n_paths: int = 4_000, horizons=(15, 30, 45, 60, 75, 90)) -> dict:
    cfg = WorldConfig()
    err = {True: {h: [] for h in horizons}, False: {h: [] for h in horizons}}
    for s in seeds:
        fut = resample_futures(cfg, s, n_futures)
        oracle_med = np.median(fut.balance_paths(90), axis=0)
        cats = fut.truths[0].transactions.select("txn_uid", "category")
        for with_sales in (True, False):
            inp = build_learned_inputs(fut.observed, cats, cfg.as_of_date, f"gap1-{s}", with_sales=with_sales)
            q50 = run_engine(inp, f"gap1-{s}", n_paths).balance_quantiles["q50"].to_numpy()
            for h in horizons:
                err[with_sales][h].append((q50[h - 1] - oracle_med[h - 1]) / 100)  # rupees
    return {("with" if k else "without") + "_future_invoices": {h: float(np.mean(v)) for h, v in d.items()}
            for k, d in err.items()}


def speed(n_paths: int = 10_000, horizon: int = 90, past_seed: int = 0) -> dict:
    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed, 0)
    cats = truth.transactions.select("txn_uid", "category")
    t = time.perf_counter()
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, "speed")
    t_build = time.perf_counter() - t
    t = time.perf_counter()
    run_engine(inp, "speed", n_paths, horizon)
    return {"build_inputs_s": t_build, "simulate_s": time.perf_counter() - t, "n_paths": n_paths, "horizon": horizon,
            "n_open_invoices": len(inp.open_invoices.buyer)}
