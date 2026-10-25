"""Obligations payload for the owner dashboard (eval/demo/obligations.json, schema obligations-v1).

    uv run --project intelligence python dashboard/owner/precompute/obligations.py

Uses Track B's public functions only and reruns the owner view's own baseline, so the figures match it.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "eval" / "models"))

SCHEMA = "obligations-v1"
OUT = ROOT / "eval" / "demo" / "obligations.json"


def main(n_paths: int = 10_000) -> None:
    from owner_view_demo import CUSHION, PAST_SEED, RUN_ID

    from matsya_intelligence.categorise import Categoriser
    from matsya_intelligence.engine import build_learned_inputs, run_engine
    from matsya_intelligence.io import adapters
    from matsya_intelligence.synth import WorldConfig, generate_world

    cfg = WorldConfig()
    obs, _ = generate_world(cfg, PAST_SEED, 0)
    txns = adapters.transactions(obs, cfg.as_of)
    overrides = adapters.corrections(obs, cfg.as_of)
    cats = Categoriser().fit(txns, overrides).predict(txns, overrides)
    inp = build_learned_inputs(obs, cats.select("txn_uid", "category"), cfg.as_of, RUN_ID, threshold_paise=CUSHION)
    run_id = f"{RUN_ID}:decide:verify"
    res = run_engine(inp, run_id, n_paths, 90, keep_flows=True)
    as_of = adapters.as_of_date(cfg.as_of)
    bal = res.balance  # (paths, 91): column d = closing balance d days after as_of
    gst = -res.flows["gst"] if "gst" in res.flows else None

    items = []
    for r in inp.features["features.obligations"].sort(["due_date", "kind"]).iter_rows(named=True):
        day = (r["due_date"] - as_of).days
        if not 1 <= day <= 90:
            continue
        item = {"kind": str(r["kind"]), "due_date": r["due_date"].isoformat(), "day": day,
                "confidence": round(float(r["confidence"]), 3), "amount_owner": str(r["amount_owner"]),
                "amount_paise": None if r["amount_paise"] is None else int(r["amount_paise"]),
                "amount_lo_paise": None, "amount_hi_paise": None}
        if r["amount_paise"] is None and gst is not None:
            g = gst[:, day]
            item["amount_paise"] = int(np.median(g))
            item["amount_lo_paise"], item["amount_hi_paise"] = (int(x) for x in np.quantile(g, [0.25, 0.75]))
        item["p_below_cushion_that_day"] = round(float((bal[:, day] < CUSHION).mean()), 4)
        item["median_balance_that_day_paise"] = int(np.median(bal[:, day]))
        items.append(item)

    purchases = None
    if "purchases" in res.flows:
        s = -res.flows["purchases"][:, 1:31].sum(axis=1)
        purchases = {"days": 30, "median_paise": int(np.median(s)),
                     "lo_paise": int(np.quantile(s, 0.25)), "hi_paise": int(np.quantile(s, 0.75))}

    out = {"schema_version": SCHEMA, "tenant_id": inp.tenant_id, "run_id": run_id, "as_of": as_of.isoformat(),
           "n_paths": n_paths, "cushion_paise": CUSHION, "items": items, "supplier_payments_30d": purchases,
           "flow_components": sorted(res.flows)}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(OUT, len(items), "obligations;", "flows:", sorted(res.flows))


if __name__ == "__main__":
    main()
