"""Naive cash forecast vs ours, both against the oracle (demo evidence).

    uv run --project intelligence python eval/models/naive_vs_ours.py [n_worlds_per_preset] [workers] [--demo-only]

--demo-only re-runs only the demo world and reuses the 30 worlds saved in eval/results/naive_vs_ours_worlds.json.

"Naive" is the same simulation engine with:
  - every open invoice paid in full on its due date (day 1 if already overdue): no learned timing and
    no never-paid share (invoice_cash replace transforms);
  - no future sales or linked purchases; supplier payments as a flat daily outflow at their historical
    average (build_learned_inputs naive_purchases);
  - no industry (sector) shocks and no sales parameter uncertainty; no calibration and no plan.
Fixed obligations, GST dates and the unscheduled-flow bootstrap are the same as ours.

"Ours" is the default learned pipeline (build_learned_inputs defaults). The calibration component
only widens the 1-30 day bands, so it does not change the medians or p_short scored here.

Oracle: the generator's true futures of the same past (N_FUTURES resampled futures); its median balance
by day and its p_short(90). Categories come from generator truth, as in the other engine checks.

Worlds: past seeds 0..n-1 of the healthy, borderline and distressed presets, threshold 0 (each preset's
own). Flags: a forecast "flags the shortfall" when its chance of running short by day 90 is at least
0.5 (the runway rule: runway inside 90 days); the truth has a shortfall when the oracle's is.
A second bar, 0.2, is reported too.

Demo world: the default (borderline) world, past seed 0, with an owner cushion DEMO_THRESHOLD. Writes
eval/demo/naive_vs_ours.json (the chart data) and eval/results/naive_vs_ours.md.
"""

import json
import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import md_table, row, write  # noqa: E402

PRESETS = ("healthy", "borderline", "distressed")
N_FUTURES, N_PATHS, H = 100, 10_000, 90
DAYS = (30, 60, 90)
FLAG_BARS = (0.5, 0.2)
DEMO = ("borderline", 0)
DEMO_THRESHOLD = 10_00_000_00  # Rs 10 lakh cushion: the true chance of running short by day 90 is 0.46 (borderline)
LIKELY_DEAD = 0.5  # our chance of never being paid at or above this: "likely dead"
DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"


def naive_inputs(obs, cats, cfg, run_id: str, threshold: int):
    """(inputs, transforms) for the naive forecast: see the module docstring."""
    from matsya_intelligence.engine import build_learned_inputs
    from matsya_intelligence.scenarios.transforms import InvoiceCash

    inp = build_learned_inputs(obs, cats, cfg.as_of_date, run_id, threshold_paise=threshold, with_sales=False,
                               naive_purchases=True, sector_shocks=False, sales_uncertainty=False)
    oi = inp.open_invoices
    ts = [InvoiceCash(invoice_id=iid, on=cfg.as_of_date + timedelta(days=int(max(oi.due_day[i], 1))),
                      amount_paise=int(oi.cash[i]), mode="replace")
          for i, iid in enumerate(inp.open_invoice_ids)]
    return inp, ts


def oracle_paths(cfg, seed: int, n: int) -> tuple[np.ndarray, object, dict]:
    """(n, H) true closing balances for days 1..H, the first future's truth, and the observed tables."""
    from matsya_intelligence.synth import generate_world

    t0 = cfg.as_of_date
    paths, first, obs = [], None, None
    for fs in range(n):
        o, tr = generate_world(cfg, seed, fs)
        if first is None:
            first, obs = tr, o
        paths.append(tr.balances.filter(pl.col("date").is_between(t0 + timedelta(days=1), t0 + timedelta(days=H)))
                     ["closing_paise"].to_numpy())
    return np.stack(paths), first, obs


def _summ(bal: np.ndarray, theta: int) -> dict:
    """bal: (paths, H) days 1..H."""
    below = np.minimum.accumulate(bal, axis=1) < theta
    q = np.percentile(bal, [5, 25, 50, 75, 95], axis=0)
    return {"p_short": below.mean(axis=0), "q": q}


def one_world(args) -> dict:
    preset, seed, threshold, detail = args
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    from matsya_intelligence.engine import build_learned_inputs, run_engine
    from matsya_intelligence.scenarios import apply_transforms
    from matsya_intelligence.synth import WorldConfig

    cfg = WorldConfig.preset(preset)
    theta = cfg.shortfall_threshold_paise if threshold is None else threshold
    truth_paths, truth, obs = oracle_paths(cfg, seed, N_FUTURES)
    cats = truth.transactions.select("txn_uid", "category")
    run = f"naive-vs-ours:{preset}:{seed}"
    ours_inp = build_learned_inputs(obs, cats, cfg.as_of_date, run, threshold_paise=theta)
    ours = run_engine(ours_inp, run, N_PATHS, H)
    n_inp, n_ts = naive_inputs(obs, cats, cfg, f"{run}:naive", theta)
    naive = run_engine(apply_transforms(n_inp, n_ts), f"{run}:naive", N_PATHS, H, seed_id=run)
    s = {"truth": _summ(truth_paths, theta), "ours": _summ(ours.balance[:, 1:], theta),
         "naive": _summ(naive.balance[:, 1:], theta)}
    out = {"preset": preset, "seed": seed, "threshold_paise": theta,
           "opening_paise": int(ours_inp.opening_balance_paise)}
    for k, v in s.items():
        out[k] = {"median": {d: float(v["q"][2][d - 1]) for d in DAYS}, "p_short_90": float(v["p_short"][H - 1])}
    if detail:
        out["series"] = {k: {"q05": v["q"][0].tolist(), "q25": v["q"][1].tolist(), "q50": v["q"][2].tolist(),
                             "q75": v["q"][3].tolist(), "q95": v["q"][4].tolist(), "p_short": v["p_short"].tolist()}
                         for k, v in s.items()}
        out["dead_bills"] = likely_dead(obs, cats, cfg, run, ours_inp, truth)
    return out


def likely_dead(obs, cats, cfg, run: str, inp, truth) -> list[dict]:
    """Open invoices naive counts as certain cash that our payment-timing model marks as likely never paid."""
    from matsya_intelligence.io.adapters import track_b_inputs
    from matsya_intelligence.timing import fit_timing

    o = track_b_inputs(obs, cfg.as_of_date)
    tm = fit_timing(o["invoice_outcomes"], o["invoices"], o["buyers"], o["complete_through"], run_id=run)
    never = dict(tm.predict(1).select("invoice_id", "p_never").iter_rows())
    names = dict(zip(obs["ref.buyers"]["buyer_id"], obs["ref.buyers"]["name"]))
    dead_true = dict(zip(truth.invoices["invoice_id"], truth.invoices["never_paid"]))
    inv = o["invoices"].filter(pl.col("invoice_id").is_in(list(inp.open_invoice_ids)))
    rows = []
    for r in inv.iter_rows(named=True):
        p = float(never.get(r["invoice_id"], 0.0))
        if p < LIKELY_DEAD:
            continue
        i = list(inp.open_invoice_ids).index(r["invoice_id"])
        rows.append({"invoice_id": r["invoice_id"], "buyer_id": r["buyer_id"], "buyer": names.get(r["buyer_id"]),
                     "amount_paise": int(inp.open_invoices.cash[i]), "gross_paise": int(r["gross_paise"]),
                     "issue_date": r["issue_date"].isoformat(), "due_date": r["due_date"].isoformat(),
                     "age_days": (cfg.as_of_date - r["issue_date"]).days,
                     "days_overdue": max(0, (cfg.as_of_date - r["due_date"]).days),
                     "p_never": p, "truly_never_paid": bool(dead_true.get(r["invoice_id"], False))})
    return sorted(rows, key=lambda x: (-x["amount_paise"], x["invoice_id"]))


def _rs_lakh(p: float) -> str:
    return f"{p / 100 / 1e5:,.2f}"


def summarise(worlds: list[dict], demo: dict) -> tuple[str, list[dict], dict]:
    n = len(worlds)
    rows, table = [], []
    err = {m: {d: np.array([abs(w[m]["median"][d] - w["truth"]["median"][d]) for w in worlds]) / 100 for d in DAYS}
           for m in ("naive", "ours")}
    perr = {m: np.array([abs(w[m]["p_short_90"] - w["truth"]["p_short_90"]) for w in worlds]) for m in ("naive", "ours")}
    rng = np.random.default_rng(20261025)
    idx = rng.integers(0, n, (5000, n))

    def ci(a, b):
        d = a - b
        lo, hi = np.percentile(d[idx].mean(axis=1), [2.5, 97.5])
        return d.mean(), lo, hi

    for d in DAYS:
        m, lo, hi = ci(err["ours"][d], err["naive"][d])
        table.append({"metric": f"median balance error, day {d} (Rs lakh)", "naive": _rs_lakh(err["naive"][d].mean() * 100),
                      "ours": _rs_lakh(err["ours"][d].mean() * 100),
                      "ours - naive (95% CI)": f"{m / 1e5:+.2f} ({lo / 1e5:+.2f} to {hi / 1e5:+.2f})",
                      "ours better in": f"{int((err['ours'][d] < err['naive'][d]).sum())}/{n}"})
        for model in ("naive", "ours"):
            rows.append(row("naive_vs_ours", "synthetic_30_worlds", "simulation engine", model, "median_abs_err_rs",
                            err[model][d].mean(), n, "naive", horizon=d))
    m, lo, hi = ci(perr["ours"], perr["naive"])
    table.append({"metric": "p_short(90) error (absolute)", "naive": f"{perr['naive'].mean():.3f}",
                  "ours": f"{perr['ours'].mean():.3f}", "ours - naive (95% CI)": f"{m:+.3f} ({lo:+.3f} to {hi:+.3f})",
                  "ours better in": f"{int((perr['ours'] < perr['naive']).sum())}/{n}"})
    for model in ("naive", "ours"):
        rows.append(row("naive_vs_ours", "synthetic_30_worlds", "simulation engine", model, "p_short_90_abs_err",
                        perr[model].mean(), n, "naive", horizon=90))
    for bar in FLAG_BARS:
        t = np.array([w["truth"]["p_short_90"] >= bar for w in worlds])
        rec = {"metric": f"flags the shortfall (bar {bar}): right / caught / false alarms", "ours - naive (95% CI)": "",
               "ours better in": f"truth has one in {int(t.sum())}/{n}"}
        for model in ("naive", "ours"):
            f = np.array([w[model]["p_short_90"] >= bar for w in worlds])
            rec[model] = f"{int((f == t).sum())}/{n} / {int((f & t).sum())}/{int(t.sum())} / {int((f & ~t).sum())}"
            rows.append(row("naive_vs_ours", "synthetic_30_worlds", "simulation engine", model, f"flag_accuracy_{bar}",
                            float((f == t).mean()), n, "naive", horizon=90))
        table.append(rec)

    per = [{"world": f"{w['preset']}:{w['seed']}", "truth p": w["truth"]["p_short_90"], "naive p": w["naive"]["p_short_90"],
            "ours p": w["ours"]["p_short_90"],
            **{f"median d{d} truth / naive / ours (Rs lakh)":
               f"{_rs_lakh(w['truth']['median'][d])} / {_rs_lakh(w['naive']['median'][d])} / {_rs_lakh(w['ours']['median'][d])}"
               for d in DAYS}} for w in worlds]

    dm = demo
    dead = dm["dead_bills"]
    names = ", ".join(sorted({x["buyer"] for x in dead}))
    md = ["# Naive vs ours against the truth\n",
          f"{n} worlds ({', '.join(PRESETS)} x past seeds), threshold 0. Oracle: {N_FUTURES} true futures per world; "
          f"engine {N_PATHS:,} paths. Errors are means over worlds of |forecast - truth|; paired bootstrap over worlds.\n",
          md_table(pl.DataFrame(table)), "",
          "Flags: the chance of running short by day 90 is at or above the bar. right = agrees with the truth; "
          "caught = flagged when the truth has a shortfall; false alarms = flagged when it does not.\n",
          f"## Demo world ({DEMO[0]}, past seed {DEMO[1]}, cushion Rs {DEMO_THRESHOLD / 1e7:,.0f} lakh)\n",
          md_table(pl.DataFrame([{"model": m, "p_short(90)": dm[m]["p_short_90"],
                                  **{f"median day {d}, Rs lakh": _rs_lakh(dm[m]["median"][d]) for d in DAYS}}
                                 for m in ("truth", "naive", "ours")])), "",
          f"Bills naive counts as certain cash that ours marks as likely dead (our chance of never being paid >= "
          f"{LIKELY_DEAD}): {len(dead)}, Rs {_rs_lakh(sum(x['amount_paise'] for x in dead))} lakh in total ({names}).\n",
          md_table(pl.DataFrame([{"buyer": x["buyer"], "amount (Rs)": f"{x['amount_paise'] / 100:,.0f}",
                                  "age (days)": x["age_days"], "days overdue": x["days_overdue"],
                                  "our chance never paid": x["p_never"], "never paid in truth": x["truly_never_paid"]}
                                 for x in dead])) if dead else "(none)", "",
          "## Per world\n", md_table(pl.DataFrame(per)), ""]
    t5 = np.array([w["truth"]["p_short_90"] >= 0.5 for w in worlds])
    key = {"n_worlds": n, "n_futures": N_FUTURES,
           "median_err_90_rs": {m: float(err[m][90].mean()) for m in ("naive", "ours")},
           "median_err_30_rs": {m: float(err[m][30].mean()) for m in ("naive", "ours")},
           "p_short_90_err": {m: float(perr[m].mean()) for m in ("naive", "ours")},
           "truth_shortfalls": int(t5.sum()),
           "caught": {m: int((np.array([w[m]["p_short_90"] >= 0.5 for w in worlds]) & t5).sum()) for m in ("naive", "ours")},
           "false_alarms": {m: int((np.array([w[m]["p_short_90"] >= 0.5 for w in worlds]) & ~t5).sum())
                            for m in ("naive", "ours")},
           "demo_dead_bills": len(dead), "demo_dead_paise": int(sum(x["amount_paise"] for x in dead))}
    return "\n".join(md) + "\n", rows, key


def chart_json(demo: dict, key: dict) -> dict:
    from matsya_intelligence.synth import WorldConfig

    t0 = WorldConfig.preset(DEMO[0]).as_of_date
    s = demo["series"]
    return {"as_of": t0.isoformat(), "threshold_paise": demo["threshold_paise"], "opening_paise": demo["opening_paise"],
            "days": list(range(1, H + 1)), "dates": [(t0 + timedelta(days=d)).isoformat() for d in range(1, H + 1)],
            "series": {k: {q: [int(round(x)) for x in v[q]] for q in ("q05", "q25", "q50", "q75", "q95")}
                       | {"p_short": [float(x) for x in v["p_short"]]} for k, v in s.items()},
            "p_short_90": {k: demo[k]["p_short_90"] for k in ("truth", "naive", "ours")},
            "dead_bills": demo["dead_bills"], "n_futures": N_FUTURES, "n_paths": N_PATHS, "across_worlds": key,
            "note": "truth = the generator's true futures of this past; bands are 5-95 and 25-75 percentiles"}


def main(n_per_preset: int = 10, workers: int = 4, demo_only: bool = False) -> None:
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    saved = Path(__file__).resolve().parents[1] / "results" / "naive_vs_ours_worlds.json"
    if demo_only:
        worlds = [{**w, **{m: {**w[m], "median": {int(k): v for k, v in w[m]["median"].items()}}
                           for m in ("truth", "naive", "ours")}} for w in json.loads(saved.read_text())]
        demo = one_world((*DEMO, DEMO_THRESHOLD, True))
    else:
        jobs = [(p, s, None, False) for p in PRESETS for s in range(n_per_preset)] + [(*DEMO, DEMO_THRESHOLD, True)]
        with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
            res = list(ex.map(one_world, jobs))
        worlds, demo = res[:-1], res[-1]
    md, rows, key = summarise(worlds, demo)
    write(rows, md, "naive_vs_ours")
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    (DEMO_DIR / "naive_vs_ours.json").write_text(json.dumps(chart_json(demo, key), indent=1))
    (Path(__file__).resolve().parents[1] / "results" / "naive_vs_ours_worlds.json").write_text(
        json.dumps(worlds, indent=1, default=str))
    print(md)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--demo-only"]
    main(*(int(a) for a in args[:2]), demo_only="--demo-only" in sys.argv)
