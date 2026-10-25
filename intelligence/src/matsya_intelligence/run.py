"""Run Track B end to end for every tenant in the configured DATA_SOURCE and write its tables.

    python -m matsya_intelligence.run [--n-paths N] [--out DIR]

Per tenant: categoriser -> features.txn_category; obligations detector, payment-timing
model, sales forecaster and shock component (inside build_learned_inputs) ->
features.obligations, features.invoice_timing, features.sales_process,
forecast.shock_state; simulation engine -> forecast.runs, balance_quantiles, shortfall;
calibration (horizons 1-30) -> forecast.calibration_state.
run_id is derived from source, tenant and as_of, so a rerun is bit-identical.

Calibration carries state from run to run: the level per horizon, the bands issued and not yet
scored, and recent outcomes. main() keeps it as JSON per tenant under <out>/calibration/. Each
run first scores the pending bands whose day's balance is now known, then issues this run's
bands from the engine's paths. The calibrated band itself has no contract table yet (question 11
in docs/track-b-decisions.md); it is kept in the state file and returned with return_inputs.
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl

from matsya_intelligence.calibrate import PRODUCTION_HORIZONS, Calibrator
from matsya_intelligence.categorise import Categoriser
from matsya_intelligence.engine import build_learned_inputs, run_engine
from matsya_intelligence.io import adapters
from matsya_intelligence.io.sink import write_outputs
from matsya_intelligence.io.source import ROOT, Source, load, load_config


def run_tenant(tables: dict[str, pl.DataFrame], tenant_id: str, as_of: date | datetime, source: str,
               n_paths: int = 10_000, return_inputs: bool = False, calibrator: Calibrator | None = None):
    """Every output table for one tenant; with return_inputs, (tables, EngineInputs, EngineResult)
    and, when a calibrator is given, the run's calibrated bands in res.run["calibration_bands"].
    The calibrator is updated in place (score what became known, then issue this run's bands)."""
    t = adapters.for_tenant(tables, tenant_id)
    run_id = f"{source}:{tenant_id}:{adapters.as_of_date(as_of).isoformat()}"
    txns = adapters.transactions(t, as_of)
    overrides = adapters.corrections(t, as_of)
    cats = Categoriser().fit(txns, overrides).predict(txns, overrides)
    inp = build_learned_inputs(t, cats.select("txn_uid", "category"), as_of, run_id)  # as_of unchanged
    day = adapters.as_of_date(as_of)
    if calibrator is not None:
        calibrator.update(day, adapters.known_balances(t, as_of))
    res = run_engine(inp, run_id, n_paths)
    out = {"features.txn_category": cats, **inp.features, **res.contract_tables()}
    if calibrator is not None:
        res.run["calibration_bands"] = calibrator.issue(day, res.balance)
        out["forecast.calibration_state"] = calibrator.state_table(tenant_id, day)
    return (out, inp, res) if return_inputs else out


def load_calibrator(state_dir: Path | None, tenant_id: str) -> Calibrator:
    path = None if state_dir is None else Path(state_dir) / f"{tenant_id}.json"
    if path is not None and path.exists():
        return Calibrator.from_json(json.loads(path.read_text()))
    return Calibrator(horizons=PRODUCTION_HORIZONS)


def save_calibrator(state_dir: Path, tenant_id: str, cal: Calibrator) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{tenant_id}.json").write_text(json.dumps(cal.to_json(), sort_keys=True))


def run_all(src: Source, n_paths: int = 10_000, state_dir: Path | None = None) -> dict[str, pl.DataFrame]:
    """state_dir: where each tenant's calibration state is read from and saved to (None: fresh, not saved)."""
    per_tenant = []
    for t in sorted(src.tables["ledger.transactions"]["tenant_id"].unique()):
        cal = load_calibrator(state_dir, t)
        per_tenant.append(run_tenant(src.tables, t, src.as_of, src.name, n_paths, calibrator=cal))
        if state_dir is not None:
            save_calibrator(state_dir, t, cal)
    return {k: pl.concat([o[k] for o in per_tenant if k in o]) for k in sorted({k for o in per_tenant for k in o})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-paths", type=int, default=10_000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = load_config()
    out = Path(a.out or cfg["outputs"]["dir"])
    out = out if out.is_absolute() else ROOT / out
    for p in write_outputs(run_all(load(cfg), a.n_paths, state_dir=out / "calibration"), out):
        print(p)


if __name__ == "__main__":
    main()
