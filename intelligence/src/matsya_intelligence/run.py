"""Run Track B end to end for every tenant in the configured DATA_SOURCE and write its tables.

    python -m matsya_intelligence.run [--n-paths N] [--out DIR]

Per tenant: categoriser -> features.txn_category; obligations detector, payment-timing
model, sales forecaster and shock component (inside build_learned_inputs) ->
features.obligations, features.invoice_timing, features.sales_process,
forecast.shock_state; simulation engine -> forecast.runs, balance_quantiles, shortfall.
run_id is derived from source, tenant and as_of, so a rerun is bit-identical.
"""

import argparse
from datetime import date, datetime
from pathlib import Path

import polars as pl

from matsya_intelligence.categorise import Categoriser
from matsya_intelligence.engine import build_learned_inputs, run_engine
from matsya_intelligence.io import adapters
from matsya_intelligence.io.sink import write_outputs
from matsya_intelligence.io.source import ROOT, Source, load, load_config


def run_tenant(tables: dict[str, pl.DataFrame], tenant_id: str, as_of: date | datetime, source: str,
               n_paths: int = 10_000, return_inputs: bool = False):
    """Every output table for one tenant; with return_inputs, (tables, EngineInputs, EngineResult)."""
    t = adapters.for_tenant(tables, tenant_id)
    run_id = f"{source}:{tenant_id}:{adapters.as_of_date(as_of).isoformat()}"
    txns = adapters.transactions(t, as_of)
    overrides = adapters.corrections(t, as_of)
    cats = Categoriser().fit(txns, overrides).predict(txns, overrides)
    inp = build_learned_inputs(t, cats.select("txn_uid", "category"), as_of, run_id)  # as_of unchanged
    res = run_engine(inp, run_id, n_paths)
    out = {"features.txn_category": cats, **inp.features, **res.contract_tables()}
    return (out, inp, res) if return_inputs else out


def run_all(src: Source, n_paths: int = 10_000) -> dict[str, pl.DataFrame]:
    per_tenant = [run_tenant(src.tables, t, src.as_of, src.name, n_paths)
                  for t in sorted(src.tables["ledger.transactions"]["tenant_id"].unique())]
    return {k: pl.concat([o[k] for o in per_tenant if k in o]) for k in sorted({k for o in per_tenant for k in o})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-paths", type=int, default=10_000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = load_config()
    out = Path(a.out or cfg["outputs"]["dir"])
    out = out if out.is_absolute() else ROOT / out
    for p in write_outputs(run_all(load(cfg), a.n_paths), out):
        print(p)


if __name__ == "__main__":
    main()
