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
from datetime import timedelta

import numpy as np
import polars as pl

from matsya_intelligence.engine.core import run_engine
from matsya_intelligence.engine.learned import build_learned_inputs
from matsya_intelligence.engine.oracle_inputs import build_true_inputs
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth import WorldConfig, generate_world, resample_futures

DAYS = (30, 60, 90)
CURATED_BORDERLINE = (0, 1, 2)  # borderline preset pasts whose oracle p_short(90) is 0.1-0.4 (see PRESETS)


HORIZONS = (15, 30, 45, 60, 75, 90)
FLOW_TYPE_OF_CATEGORY = {
    "customer_receipt": "receipts", "supplier_payment": "purchases", "gst_payment": "gst",
    **{c: "fixed" for c in ("salary", "rent", "emi", "professional_fee", "tds_payment", "pf_esi")},
}
FLOW_TYPE_OF_COMPONENT = {
    "open_invoice_receipts": "receipts", "future_invoice_receipts": "receipts", "purchases": "purchases",
    "gst": "gst", "fixed_obligations": "fixed", "unscheduled": "unscheduled", "overrides": "overrides",
}
FLOW_TYPES = ("receipts", "purchases", "gst", "fixed", "unscheduled")


def oracle_summary(past_seed: int, n_futures: int = 200) -> dict:
    """Oracle for one borderline past: p_short by day, median balance by day, and the
    mean cumulative flow by type at each horizon (means add up; medians do not)."""
    cfg = WorldConfig()
    t0 = cfg.as_of_date
    cum = {t: np.zeros(len(HORIZONS)) for t in FLOW_TYPES}
    paths = []
    for fs in range(n_futures):  # streamed: holding 200 truths per worker exhausts memory
        _, tr = generate_world(cfg, past_seed, fs)
        paths.append(tr.balances.filter(pl.col("date").is_between(t0 + timedelta(days=1), t0 + timedelta(days=90)))
                     ["closing_paise"].to_numpy())
        f = tr.transactions.filter(pl.col("value_date") > t0).with_columns(
            ((pl.col("value_date") - pl.lit(t0)).dt.total_days()).alias("day"),
            pl.col("category").replace_strict(FLOW_TYPE_OF_CATEGORY, default="unscheduled").alias("type"))
        for t in FLOW_TYPES:
            g = f.filter(pl.col("type") == t)
            for i, h in enumerate(HORIZONS):
                cum[t][i] += g.filter(pl.col("day") <= h)["amount_paise"].sum() / n_futures
    paths = np.stack(paths)
    below = np.minimum.accumulate(paths, axis=1) < cfg.shortfall_threshold_paise
    return {"past_seed": past_seed, "n_futures": n_futures, "p_short": below.mean(axis=0),
            "median": np.median(paths, axis=0), "flow_cum_mean": cum}


def oracle_cache(seeds=range(100), n_futures: int = 200, path: str | None = None, workers: int = 4) -> list[dict]:
    """oracle_summary for each past, loaded from path if it exists, else computed (and saved there)."""
    import multiprocessing as mp
    import os
    import pickle
    from concurrent.futures import ProcessPoolExecutor

    if path and os.path.exists(path):
        return pickle.load(open(path, "rb"))
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        out = list(ex.map(oracle_summary, list(seeds), [n_futures] * len(seeds)))
    if path:
        pickle.dump(out, open(path, "wb"))
    return out


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


def _learned_one(args) -> dict:
    past_seed, n_paths, flags = args
    cfg = WorldConfig()
    flags = dict(flags)
    if flags.pop("true_priors", False):  # diagnostic: the generator's type hyperpriors as timing priors
        from matsya_intelligence.timing.evaluate import true_hyperpriors

        flags["timing_priors"] = true_hyperpriors(cfg)
    obs, truth = generate_world(cfg, past_seed, 0)
    cats = truth.transactions.select("txn_uid", "category")
    run_id = f"cal-{past_seed}"
    true_opening = flags.pop("true_opening", False)  # diagnostic: the generator's true balance at as_of
    inp = build_learned_inputs(obs, cats, cfg.as_of_date, run_id, **flags)
    if true_opening:
        inp.opening_balance_paise = cfg.as_of_balance_paise
    res = run_engine(inp, run_id, n_paths, keep_flows=True)
    cum = {t: np.zeros(len(HORIZONS)) for t in FLOW_TYPES}
    for comp, arr in res.flows.items():
        t = FLOW_TYPE_OF_COMPONENT[comp]
        if t in cum:
            c = np.cumsum(arr, axis=1).mean(axis=0)
            cum[t] += np.array([c[h] for h in HORIZONS])
    return {"past_seed": past_seed, "p_short": res.shortfall["p_short"].to_numpy(),
            "median": res.balance_quantiles["q50"].to_numpy(), "flow_cum_mean": cum}


def learned_calibration(oracle: list[dict], n_paths: int = 4000, workers: int = 12, **flags) -> dict:
    """Learned mode on every cached oracle past: calibration of p_short(90), mean gaps, and a
    flow-type breakdown of the balance gap (mean cumulative flows, engine minus oracle, Rs)."""
    import multiprocessing as mp
    import os
    from concurrent.futures import ProcessPoolExecutor

    # spawn, not fork: forking after Polars has started its thread pool crashes workers
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        eng = list(ex.map(_learned_one, [(o["past_seed"], n_paths, flags) for o in oracle]))
    pred = np.array([e["p_short"][89] for e in eng])
    true = np.array([o["p_short"][89] for o in oracle])
    bins = [0, 0.05, 0.2, 0.4, 0.6, 0.8, 1.0001]
    table = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (pred >= lo) & (pred < hi)
        if m.any():
            table.append({"bin": f"[{lo:.2f}, {min(hi, 1):.2f})", "n": int(m.sum()),
                          "mean_predicted": float(pred[m].mean()), "mean_oracle": float(true[m].mean())})
    hidx = [h - 1 for h in HORIZONS]
    median_gap = np.mean([e["median"][hidx] - o["median"][hidx] for e, o in zip(eng, oracle)], axis=0) / 100
    breakdown = {t: np.mean([e["flow_cum_mean"][t] - o["flow_cum_mean"][t] for e, o in zip(eng, oracle)], axis=0) / 100
                 for t in FLOW_TYPES}
    return {
        "n_pasts": len(oracle), "n_futures": oracle[0]["n_futures"], "flags": flags, "calibration_90": table,
        "mean_gap_90": float((pred - true).mean()), "mean_abs_gap_90": float(np.abs(pred - true).mean()),
        "corr_90": float(np.corrcoef(pred, true)[0, 1]),
        "median_gap_rs": dict(zip(HORIZONS, median_gap)), "flow_gap_rs": breakdown,
    }


def print_calibration(r: dict) -> None:
    print(f"flags {r['flags']}: {r['n_pasts']} pasts, oracle {r['n_futures']} futures; "
          f"mean gap {r['mean_gap_90']:+.3f}, mean |gap| {r['mean_abs_gap_90']:.3f}, corr {r['corr_90']:.3f}")
    print("| bin of predicted p_short(90) | n | mean predicted | mean oracle |")
    print("|---|---|---|---|")
    for b in r["calibration_90"]:
        print(f"| {b['bin']} | {b['n']} | {b['mean_predicted']:.3f} | {b['mean_oracle']:.3f} |")
    print("| horizon | median gap | " + " | ".join(FLOW_TYPES) + " |")
    print("|---|---|" + "---|" * len(FLOW_TYPES))
    for i, h in enumerate(HORIZONS):
        print(f"| {h} | {r['median_gap_rs'][h]:,.0f} | " + " | ".join(f"{r['flow_gap_rs'][t][i]:,.0f}" for t in FLOW_TYPES) + " |")
