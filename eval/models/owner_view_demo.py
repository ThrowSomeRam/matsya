"""Precompute the owner view payload for the demo world: baseline, default plan, every preset.

    uv run --project intelligence python eval/models/owner_view_demo.py

Demo world: the default (borderline) world, past seed 2, with no owner cushion, where the true
chance of running short by day 90 is about 0.27 (the demo world of naive_vs_ours.py). Small company tenant, overdraft limit
Rs 10 lakh, suppliers S1 micro 50% and S2 not MSME 30% on 30-day written terms. Categories come from the
categoriser, as in the production run; no calibration history (a first run). 10,000 paths.

Writes eval/demo/owner_view.json (the OwnerView payload the API serves) and eval/demo/owner_view_run.json
(seeds, solver status and timings for the record).
"""

import json
import sys
import time
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[1] / "demo"
PAST_SEED, CUSHION = 2, 0
RUN_ID = "demo:borderline:2"


def main(n_paths: int = 10_000) -> None:
    from matsya_intelligence.categorise import Categoriser
    from matsya_intelligence.decide import Supplier, decision_inputs
    from matsya_intelligence.engine import build_learned_inputs
    from matsya_intelligence.io import adapters
    from matsya_intelligence.present.owner_view import build_owner_view
    from matsya_intelligence.present.tenant_run import compute_tenant_run
    from matsya_intelligence.synth import WorldConfig, generate_world

    t0 = time.perf_counter()
    cfg = WorldConfig()
    obs, _ = generate_world(cfg, PAST_SEED, 0)
    txns = adapters.transactions(obs, cfg.as_of)
    overrides = adapters.corrections(obs, cfg.as_of)
    cats = Categoriser().fit(txns, overrides).predict(txns, overrides)
    inp = build_learned_inputs(obs, cats.select("txn_uid", "category"), cfg.as_of, RUN_ID, threshold_paise=CUSHION)
    dinp = decision_inputs(obs, inp, "small", overdraft_limit_paise=10_00_000_00,
                           suppliers=[Supplier("S1", 0.5, "micro", 30), Supplier("S2", 0.3, "none", 30)],
                           entity_type="company")
    tr = compute_tenant_run(obs, inp, dinp, RUN_ID, n_paths=n_paths)
    view = build_owner_view(tr)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    (DEMO_DIR / "owner_view.json").write_text(view.model_dump_json(indent=1) + "\n")
    rec = {"run_id": tr.run_id, "plan_record": tr.plan.record, "solver": tr.plan.solver,
           "attribution_seconds": tr.attribution.seconds, "seconds": time.perf_counter() - t0, "n_paths": n_paths,
           "breaking_points": {k: None if bp is None else {"magnitude": bp.magnitude, "monotone": bp.monotone,
                                                           "evaluations": bp.evaluations}
                               for k, _, _, bp in tr.scenarios}}
    (DEMO_DIR / "owner_view_run.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
    print(view.headline.model_dump_json(indent=1))
    print(view.plan.summary, view.plan.caveat)
    print(f"{time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:2]))
