"""Decision engine sensitivity to the MSMED claim settings, on the default world.

World: past seed 0 (borderline preset), learned inputs, Rs 20 lakh cushion so the world is risky,
small company tenant, overdraft limit Rs 10 lakh, suppliers S1 micro 50% and S2 not MSME 30% on
30-day written terms; eps 0.05 at day 90, 1,000 solve paths, 10,000 fresh paths. Every run uses
the same seeds (common random numbers); only the constants override changes.

Runs: default; claims off; recovery_share 0.25 / 0.5 / 0.75; relationship penalty 0.05 / 0.10 /
0.20 of principal.

Run: uv run --project intelligence python eval/models/decision_sensitivity.py [workers]
"""

import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import md_table, row, write  # noqa: E402

RUN = "decision-sensitivity"
CUSHION = 20_00_000_00
N_SUB, N_VER = 1000, 10_000
RUNS = {
    "default": {},
    "claims off": {"msmed_claim.enabled": False},
    "recovery 0.25": {"msmed_claim.recovery_share": 0.25},
    "recovery 0.50": {"msmed_claim.recovery_share": 0.5},
    "recovery 0.75": {"msmed_claim.recovery_share": 0.75},
    "penalty 0.05": {"msmed_claim.relationship_penalty_share": 0.05},
    "penalty 0.10": {"msmed_claim.relationship_penalty_share": 0.10},
    "penalty 0.20": {"msmed_claim.relationship_penalty_share": 0.20},
}


def one(name: str) -> dict:
    from matsya_intelligence.decide import Supplier, decide, decision_inputs
    from matsya_intelligence.engine import build_learned_inputs
    from matsya_intelligence.synth import WorldConfig, generate_world

    cfg = WorldConfig()
    obs, truth = generate_world(cfg, past_seed=0, future_seed=0)
    inp = build_learned_inputs(obs, truth.transactions.select("txn_uid", "category"), cfg.as_of_date, RUN,
                               threshold_paise=CUSHION)
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=10_00_000_00,
                           suppliers=[Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)],
                           entity_type="company")
    r = decide(inp, dinp, RUN, n_sub=N_SUB, n_verify=N_VER, constants_overrides=RUNS[name])
    worth = r.money_owed.filter(pl.col("net_value_paise") > 0)
    return {"run": name, "actions": [f"{a.kind} {a.target_id[:12]}" + (f" x{k}" if a.kind == "overdraft" else "")
                                     for a, k in r.chosen],
            "net_paise": sum(a.cost(k) for a, k in r.chosen), "clamped_paise": int(r.plan["cost_paise"].sum()),
            "n_claims": worth.height, "claims_net_paise": int(worth["net_value_paise"].sum()),
            "solver": "time limit" if r.solver["time_limited"] else "optimal",
            "p_short": r.p_short_verified, "p_short_sub": r.p_short_sub, "target_met": r.target_met}


def main(workers: int = 4) -> None:
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        res = dict(zip(RUNS, ex.map(one, RUNS)))
    d = res["default"]
    table = [{"run": n, "actions": len(x["actions"]), "net cost Rs": x["net_paise"] / 100,
              "vs default Rs": (x["net_paise"] - d["net_paise"]) / 100, "clamped Rs": x["clamped_paise"] / 100,
              "money owed worth claiming": x["n_claims"], "its net value Rs": x["claims_net_paise"] / 100,
              "solver": x["solver"], "p_short fresh": f"{x['p_short']:.4f}",
              "plan same as default": sorted(x["actions"]) == sorted(d["actions"])} for n, x in res.items()]
    lines = ["# Decision engine: sensitivity to MSMED claim settings\n",
             f"Default world (past seed 0), Rs 20 lakh cushion, small company tenant, overdraft limit Rs 10 lakh, S1 micro "
             f"50% and S2 not MSME 30% on 30-day written terms; eps 0.05 at day 90; {N_SUB:,} solve and {N_VER:,} fresh "
             "paths, same seeds in every run. One world: illustrative, not a population result.\n",
             md_table(pl.DataFrame(table), floats=2), "", "## Plans\n"]
    for n, x in res.items():
        lines.append(f"- **{n}**: " + "; ".join(x["actions"]))
    rows = [row("decision_sensitivity", "synthetic_default_world", "decision engine", n, metric, v, N_VER, "default",
                horizon=90)
            for n, x in res.items() for metric, v in (("net_cost_rs", x["net_paise"] / 100), ("p_short_90", x["p_short"]),
                                                      ("n_claims", x["n_claims"]))]
    md = "\n".join(lines) + "\n"
    write(rows, md, "decision_sensitivity")
    print(md)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
