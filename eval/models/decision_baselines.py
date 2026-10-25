"""Decision engine against its baselines across 30 generator worlds.

Worlds: past seeds 0-9 for each WorldConfig preset (healthy, borderline, distressed), learned
inputs, small company tenant, overdraft limit Rs 10 lakh, suppliers S1 micro 50% and S2 not MSME
30%, both on 30-day written terms; eps 0.05 at day 90, 1,000 solve paths, 10,000 fresh paths.

Risk level: the shortfall threshold is the owner cushion (0 here). To put each world near
p_short 0.5, a pilot run on its own seed (2,000 paths, never reused) gives the median over paths
of the do-nothing 90-day minimum balance, and the opening balance is lowered by it. That is the
same arithmetic as a threshold at that median (the opening balance adds a constant to every
path) without a non-zero cushion. The realised do-nothing p_short on the fresh paths is reported.

The plan is solved at 0.8 eps in sample (decision.solve_eps_factor) and judged at eps on fresh
paths. MSMED claims are reported as money owed, not planned. Solver status and MIP gap per plan.

Strategies, all on the same fresh paths: do nothing, discount every eligible invoice, chase the
largest buyer, the plan. Costs: net (signed, claims can be negative) and clamped (as stored).

Run: uv run --project intelligence python eval/models/decision_baselines.py [n_worlds_per_preset] [workers]
"""

import json
import math
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import md_table, row, write  # noqa: E402

PRESETS = ("healthy", "borderline", "distressed")
EPS, DAY, N_SUB, N_VER, N_PILOT = 0.05, 90, 1000, 10_000, 2000
OVERDRAFT_LIMIT = 10_00_000_00
STRATEGIES = ("do_nothing", "discount_all_eligible", "chase_largest_buyer", "plan")


def one_world(args) -> dict:
    preset, seed, overrides = args
    from matsya_intelligence.decide import Supplier, decide, decision_inputs
    from matsya_intelligence.engine import build_learned_inputs, run_engine
    from matsya_intelligence.synth import WorldConfig, generate_world

    cfg = WorldConfig.preset(preset)
    obs, truth = generate_world(cfg, past_seed=seed, future_seed=seed)
    run = f"decision-baselines:{preset}:{seed}"
    inp = build_learned_inputs(obs, truth.transactions.select("txn_uid", "category"), cfg.as_of_date, run)
    pilot = run_engine(inp, f"{run}:pilot", N_PILOT)
    shift = int(np.median(pilot.balance[:, 1:DAY + 1].min(axis=1)))
    inp.opening_balance_paise -= shift
    suppliers = [Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)]
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=OVERDRAFT_LIMIT, suppliers=suppliers,
                           entity_type="company", eps=EPS, day=DAY)
    t = time.perf_counter()
    res = decide(inp, dinp, run, n_sub=N_SUB, n_verify=N_VER, constants_overrides=overrides, method="chance")
    elapsed = time.perf_counter() - t
    b = {r["baseline"]: r for r in res.baselines.iter_rows(named=True)}
    clamped = {"do_nothing": 0, "plan": sum(max(a.cost(k), 0) for a, k in res.chosen)}
    out = {"preset": preset, "seed": seed, "opening_shift_paise": shift, "target_met": res.target_met,
           "p_short_sub": res.p_short_sub, "p_verified_se": res.p_short_verified_se, "seconds": elapsed,
           "n_candidates": res.n_candidates, "plan_actions": [f"{a.kind}:{a.target_id}x{k}" for a, k in res.chosen],
           "solver_status": res.solver["status"], "time_limited": res.solver["time_limited"],
           "mip_gap": res.solver["mip_gap"], "eps_solve": res.eps_solve,
           "money_owed_n": res.money_owed.height,
           "money_owed_worth_claiming_n": int((res.money_owed["net_value_paise"] > 0).sum()),
           "money_owed_net_paise": int(res.money_owed["net_value_paise"].clip(lower_bound=0).sum())}
    for s in STRATEGIES:
        r = b.get(s)
        out[s] = None if r is None else {
            "n_actions": r["n_actions"], "net_cost_paise": r["cost_paise"],
            "clamped_cost_paise": clamped.get(s, r["cost_paise"]),  # discounts and chase are never negative
            "p_short": r["p_short"], "short_days": r["expected_shortfall_days"]}
    return out


def summarise(worlds: list[dict]) -> tuple[str, list[dict]]:
    n = len(worlds)
    se_target = math.sqrt(EPS * (1 - EPS) / N_SUB) + math.sqrt(EPS * (1 - EPS) / N_VER)
    lines = ["# Decision engine against baselines across worlds\n",
             f"{n} worlds ({', '.join(PRESETS)} presets x past seeds), eps {EPS} at day {DAY}, {N_SUB:,} solve paths, "
             f"{N_VER:,} fresh paths per world. Small company tenant, overdraft limit Rs 10 lakh, suppliers S1 micro 50% "
             "and S2 not MSME 30% on 30-day written terms. Opening balance lowered by the pilot median 90-day minimum "
             "(threshold stays 0). Costs in Rs; mean (sd) and median over worlds where the strategy exists.\n"]
    rows, table = [], []
    for s in STRATEGIES:
        xs = [w[s] for w in worlds if w[s] is not None]
        if not xs:
            continue
        f = lambda k: np.array([x[k] for x in xs], dtype=float)  # noqa: E731
        net, cl, p, d = f("net_cost_paise") / 100, f("clamped_cost_paise") / 100, f("p_short"), f("short_days")
        table.append({"strategy": s, "worlds": len(xs), "net cost mean (sd)": f"{net.mean():,.0f} ({net.std(ddof=1):,.0f})",
                      "net cost median": f"{np.median(net):,.0f}", "clamped cost mean": f"{cl.mean():,.0f}",
                      "p_short mean (sd)": f"{p.mean():.3f} ({p.std(ddof=1):.3f})",
                      "days below mean": f"{d.mean():.2f}"})
        for metric, v in (("net_cost_rs", net), ("clamped_cost_rs", cl), ("p_short_90", p), ("days_below_90", d)):
            rows.append(row("decision_baselines", "synthetic_30_worlds", "decision engine", s, metric, v.mean(), len(xs),
                            "do_nothing", horizon=DAY, ci=(np.percentile(v, 2.5), np.percentile(v, 97.5)),
                            note="mean over worlds; ci = 2.5-97.5 percentiles over worlds"))
    lines.append(md_table(pl.DataFrame(table)))

    p0 = np.array([w["do_nothing"]["p_short"] for w in worlds])
    pv = np.array([w["plan"]["p_short"] for w in worlds])
    unreachable = sum(not w["target_met"] for w in worlds)
    strict = int((pv <= EPS).sum())
    within = int((pv <= EPS + 2 * se_target).sum())
    in_band = int(((p0 >= 0.2) & (p0 <= 0.8)).sum())
    lines += ["",
              f"- Do-nothing p_short(90) on fresh paths: mean {p0.mean():.3f}, range {p0.min():.3f}-{p0.max():.3f}; "
              f"{in_band}/{n} worlds inside 0.2-0.8.",
              f"- Plan meets eps strictly (fresh p_short <= {EPS}): {strict}/{n}. Within Monte Carlo error "
              f"(<= eps + 2 x {se_target:.4f}, the solve and fresh-path se at eps): {within}/{n}.",
              f"- Target unreachable with the available actions: {unreachable}/{n}.",
              f"- Solver stopped at the time limit: {sum(w['time_limited'] for w in worlds)}/{n}; MIP gap median "
              f"{np.median([w['mip_gap'] for w in worlds if w['mip_gap'] is not None] or [np.nan]):.4f}, max "
              f"{max([w['mip_gap'] for w in worlds if w['mip_gap'] is not None] or [np.nan]):.4f}.",
              f"- Money owed (MSMED claims, reported apart from the plan): {sum(w['money_owed_n'] for w in worlds)} "
              f"invoices across worlds, {sum(w['money_owed_worth_claiming_n'] for w in worlds)} worth claiming "
              f"(net value after tax and penalty > 0), Rs {sum(w['money_owed_net_paise'] for w in worlds) / 100:,.0f} "
              "net in total.",
              f"- Solve time per world: median {np.median([w['seconds'] for w in worlds]):.0f} s, "
              f"max {max(w['seconds'] for w in worlds):.0f} s.", ""]
    rows += [row("decision_baselines", "synthetic_30_worlds", "decision engine", "plan", "meets_eps_strict", strict / n, n,
                 "do_nothing", horizon=DAY),
             row("decision_baselines", "synthetic_30_worlds", "decision engine", "plan", "meets_eps_within_mc", within / n, n,
                 "do_nothing", horizon=DAY),
             row("decision_baselines", "synthetic_30_worlds", "decision engine", "plan", "target_unreachable",
                 unreachable / n, n, "do_nothing", horizon=DAY),
             row("decision_baselines", "synthetic_30_worlds", "decision engine", "plan", "time_limited",
                 sum(w["time_limited"] for w in worlds) / n, n, "do_nothing", horizon=DAY)]

    # Losses: the plan misses eps beyond Monte Carlo error, or a baseline reaches the plan's p_short (or eps)
    # at no more cost.
    lines += ["## Per world\n", "Loss: plan above eps + 2 se on fresh paths, or a baseline at or below max(eps, plan p_short) "
              "for no more net cost.\n"]
    per = []
    for w in worlds:
        plan = w["plan"]
        loss = []
        if plan["p_short"] > EPS + 2 * se_target:
            loss.append("misses eps")
        for s in STRATEGIES[1:3]:
            x = w[s]
            if x is not None and x["p_short"] <= max(EPS, plan["p_short"]) and x["net_cost_paise"] <= plan["net_cost_paise"]:
                loss.append(f"beaten by {s}")
        per.append({"world": f"{w['preset']}:{w['seed']}", "do-nothing p": w["do_nothing"]["p_short"],
                    "plan p (fresh)": plan["p_short"], "plan p (solve)": w["p_short_sub"],
                    "plan net Rs": plan["net_cost_paise"] / 100, "plan clamped Rs": plan["clamped_cost_paise"] / 100,
                    "actions": plan["n_actions"], "target met": w["target_met"],
                    "solver": "time limit" if w["time_limited"] else "optimal",
                    "gap": "" if w["mip_gap"] is None else f"{w['mip_gap']:.4f}", "loss": ", ".join(loss) or "-"})
    lines.append(md_table(pl.DataFrame(per)))
    losses = [p for p in per if p["loss"] != "-"]
    lines += ["", f"Losses: {len(losses)}/{n}." + ("".join(f"\n- {p['world']}: {p['loss']}" for p in losses))]
    return "\n".join(lines) + "\n", rows


def main(n_per_preset: int = 10, workers: int = 4, variant: str | None = None) -> None:
    """variant: JSON of decision constants overrides (e.g. '{"chase.max_buyers": 3}'). A variant run
    prints its summary and keeps its per-world JSON next to the results, but does not write the
    results table or the markdown (those hold the default settings)."""
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    overrides = json.loads(variant) if variant else None
    jobs = [(p, s, overrides) for p in PRESETS for s in range(n_per_preset)]
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        worlds = list(ex.map(one_world, jobs))
    tag = "" if not overrides else "_" + "_".join(f"{k}-{v}" for k, v in sorted(overrides.items())).replace(".", "-")
    raw = Path(__file__).resolve().parents[1] / "results" / f"decision_baselines{tag}_worlds.json"
    raw.write_text(json.dumps(worlds, indent=1, default=str))
    md, rows = summarise(worlds)
    if not overrides:
        write(rows, md, "decision_baselines")
    print(md)


if __name__ == "__main__":
    a = sys.argv[1:]
    main(*(int(x) for x in a[:2]), *(a[2:3]))
