"""Decision engine: CVaR plan against the chance-constrained plan across the 30 worlds, plus a stress test.

Same worlds, seeds and owner inputs as decision_baselines.py (past seeds 0-9 of the healthy, borderline
and distressed presets; opening balance lowered by the pilot median 90-day minimum, threshold 0; small
company tenant, overdraft limit Rs 10 lakh, S1 micro 50% and S2 not MSME 30% on 30-day written terms).
Both plans solve on the same 1,000 paths and are checked on the same 10,000 fresh paths.

- chance: p_short(90) <= eps (0.05), solved at 0.8 eps in sample.
- cvar: CVaR_alpha of the 90-day shortfall depth <= budget (decision.cvar, defaults alpha 0.05, budget 0).

Per plan on fresh paths: cost, p_short(90), expected shortfall (conditional on a breach, and over all
paths), the average shortfall in the worst 5% of futures, solve time and solver status, and whether the
owner's target (p_short(90) <= eps) is met.

Stress: every plan (and doing nothing) on futures from a pessimistic model, all payment chances x 0.85
and the order rate x 0.9 (scenario transforms on the same fresh seed). The plans are not re-solved.

Run: uv run --project intelligence python eval/models/decision_cvar.py [n_worlds_per_preset] [workers]
"""

import json
import math
import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import md_table, row, write  # noqa: E402

PRESETS = ("healthy", "borderline", "distressed")
EPS, DAY, N_SUB, N_VER, N_PILOT, ALPHA = 0.05, 90, 1000, 10_000, 2000, 0.05
OVERDRAFT_LIMIT = 10_00_000_00
METHODS = ("chance", "cvar")
STRESS = {"pay": 0.85, "orders": 0.9}


def one_world(args) -> dict:
    preset, seed = args
    from matsya_intelligence.decide import Supplier, decide, decision_inputs
    from matsya_intelligence.decide.plan import shortfall_stats
    from matsya_intelligence.engine import build_learned_inputs, run_engine
    from matsya_intelligence.scenarios import RateMultiplier, apply_transforms
    from matsya_intelligence.synth import WorldConfig, generate_world

    cfg = WorldConfig.preset(preset)
    obs, truth = generate_world(cfg, past_seed=seed, future_seed=seed)
    run = f"decision-baselines:{preset}:{seed}"
    inp = build_learned_inputs(obs, truth.transactions.select("txn_uid", "category"), cfg.as_of_date, run)
    pilot = run_engine(inp, f"{run}:pilot", N_PILOT)
    inp.opening_balance_paise -= int(np.median(pilot.balance[:, 1:DAY + 1].min(axis=1)))
    suppliers = [Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)]
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=OVERDRAFT_LIMIT, suppliers=suppliers,
                           entity_type="company", eps=EPS, day=DAY)
    stress = [RateMultiplier(target="all_buyers", factor=STRESS["pay"]),
              RateMultiplier(target="orders", factor=STRESS["orders"])]
    ver_id = f"{run}:decide:verify"
    theta = inp.threshold_paise

    def stressed(ts: list, tag: str) -> dict:
        r = run_engine(apply_transforms(inp, stress + ts), f"{ver_id}:stress:{tag}", N_VER, 90, seed_id=ver_id)
        return shortfall_stats(r.balance, theta, DAY, ALPHA)

    out = {"preset": preset, "seed": seed}
    for m in METHODS:
        res = decide(inp, dinp, run, n_sub=N_SUB, n_verify=N_VER, method=m)
        if m == "chance":
            out["do_nothing"] = {**res.fresh["do_nothing"], "stress": stressed([], "do_nothing")}
        out[m] = {**res.fresh["plan"], "cost_paise": res.cost_paise, "n_actions": len(res.chosen),
                  "solve_s": res.solver["seconds"], "solver_status": res.solver["status"],
                  "solver_message": res.solver["message"], "time_limited": res.solver["time_limited"],
                  "mip_gap": res.solver["mip_gap"], "budget_met_in_sample": res.target_met,
                  "cvar_sub_paise": res.cvar_sub_paise, "n_candidates": res.n_candidates, "n_rows": res.n_rows,
                  "actions": [f"{a.kind}:{a.target_id}x{k}" for a, k in res.chosen],
                  "stress": stressed(res.transforms, m)}
    return out


def summarise(worlds: list[dict]) -> tuple[str, list[dict], dict]:
    n = len(worlds)
    se = math.sqrt(EPS * (1 - EPS) / N_VER)
    table, stress_table, rows = [], [], []
    for s in ("do_nothing",) + METHODS:
        f = lambda k: np.array([x[s][k] for x in worlds], dtype=float)  # noqa: E731
        p, esc, esu, cv = f("p_short"), f("es_cond_paise") / 100, f("es_uncond_paise") / 100, f("cvar_paise") / 100
        cost = np.zeros(n) if s == "do_nothing" else f("cost_paise") / 100
        rec = {"plan": s, "cost mean (median), Rs": f"{cost.mean():,.0f} ({np.median(cost):,.0f})",
               "p_short(90) mean": f"{p.mean():.4f}",
               "meets target (p <= 0.05)": f"{int((p <= EPS).sum())}/{n}",
               "within MC error": f"{int((p <= EPS + 2 * se).sum())}/{n}",
               "expected shortfall if short, Rs": f"{np.nanmean(np.where(p > 0, esc, np.nan)):,.0f}",
               "expected shortfall over all futures, Rs": f"{esu.mean():,.0f}",
               "worst-5% average shortfall, Rs": f"{cv.mean():,.0f}"}
        if s != "do_nothing":
            t = f("solve_s")
            rec["solve s median (max)"] = f"{np.median(t):.1f} ({t.max():.1f})"
            rec["optimal / time limit"] = (f"{sum(not x[s]['time_limited'] for x in worlds)}/"
                                           f"{sum(x[s]['time_limited'] for x in worlds)}")
        table.append(rec)
        sp = np.array([x[s]["stress"]["p_short"] for x in worlds])
        scv = np.array([x[s]["stress"]["cvar_paise"] for x in worlds]) / 100
        stress_table.append({"plan": s, "p_short(90) normal": f"{p.mean():.4f}", "p_short(90) stressed": f"{sp.mean():.4f}",
                             "change": f"{(sp - p).mean():+.4f}",
                             "meets target stressed": f"{int((sp <= EPS).sum())}/{n}",
                             "worst-5% normal, Rs": f"{cv.mean():,.0f}", "worst-5% stressed, Rs": f"{scv.mean():,.0f}",
                             "change, Rs": f"{(scv - cv).mean():+,.0f}"})
        for metric, v in (("cost_rs", cost), ("p_short_90", p), ("es_uncond_rs", esu), ("cvar05_rs", cv),
                          ("meets_eps", (p <= EPS).astype(float)), ("stress_p_short_90", sp), ("stress_cvar05_rs", scv)):
            rows.append(row("decision_cvar", "synthetic_30_worlds", "decision engine", s, metric, v.mean(), n,
                            "chance" if s == "cvar" else "do_nothing", horizon=DAY,
                            ci=(np.percentile(v, 2.5), np.percentile(v, 97.5)),
                            note="mean over worlds; ci = 2.5-97.5 percentiles over worlds"))
    hits = {m: int(sum(x[m]["p_short"] <= EPS for x in worlds)) for m in METHODS}
    dc = np.array([x["cvar"]["cost_paise"] - x["chance"]["cost_paise"] for x in worlds]) / 100
    rng = np.random.default_rng(20261025)
    bm = dc[rng.integers(0, n, (5000, n))].mean(axis=1)
    lo, hi = np.percentile(bm, [2.5, 97.5])
    default = "cvar" if hits["cvar"] >= hits["chance"] else "chance"
    md = ["# Decision engine: CVaR plan vs chance-constrained plan\n",
          f"{n} worlds ({', '.join(PRESETS)} x past seeds 0-{n // len(PRESETS) - 1}), {N_SUB:,} solve paths, {N_VER:,} "
          f"fresh paths. Owner target p_short(90) <= {EPS}; CVaR alpha {ALPHA}, budget Rs 0. Means over worlds.\n",
          md_table(pl.DataFrame(table)), "",
          f"Paired cost difference, CVaR minus chance-constrained: mean Rs {dc.mean():+,.0f} (95% CI {lo:+,.0f} to "
          f"{hi:+,.0f}); CVaR dearer in {int((dc > 0).sum())}/{n} worlds.",
          f"Owner target met on fresh paths: chance-constrained {hits['chance']}/{n}, CVaR {hits['cvar']}/{n}. "
          f"Default plan by the rule set in advance: **{default}**.", "",
          f"## Stress: payment chances x {STRESS['pay']}, order rate x {STRESS['orders']} (plans not re-solved)\n",
          md_table(pl.DataFrame(stress_table)), ""]
    return "\n".join(md) + "\n", rows, {"default": default, "hits": hits, "cost_diff_mean_rs": float(dc.mean()),
                                         "cost_diff_ci_rs": [float(lo), float(hi)]}


def main(n_per_preset: int = 10, workers: int = 4) -> None:
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    jobs = [(p, s) for p in PRESETS for s in range(n_per_preset)]
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        worlds = list(ex.map(one_world, jobs))
    (Path(__file__).resolve().parents[1] / "results" / "decision_cvar_worlds.json").write_text(
        json.dumps(worlds, indent=1, default=str))
    md, rows, verdict = summarise(worlds)
    write(rows, md, "decision_cvar")
    print(md)
    print(json.dumps(verdict))


if __name__ == "__main__":
    main(*(int(x) for x in sys.argv[1:3]))
