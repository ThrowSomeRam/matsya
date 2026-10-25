"""Everything the owner view reads for one tenant at one as_of, computed once.

compute_tenant_run runs the simulation engine (baseline), the decision engine (default plan),
the scenario engine (every preset, with its breaking point) and attribution, all on common
random numbers:

- the baseline is the decision engine's fresh-path do-nothing run (seed `<run>:decide:verify`), so
  the plan's "risk after" figures and the baseline share every random draw;
- each preset runs against that baseline (seed_id = the baseline's run_id);
- attribution runs on its own seed (`<run>:attribute`); its contributions are differences on that seed.

No LLM and no formatting here; present.owner_view turns a TenantRun into the payload.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import polars as pl

from matsya_intelligence.attribute.drivers import AttributionResult, attribute
from matsya_intelligence.calibrate import PRODUCTION_HORIZONS, Calibrator
from matsya_intelligence.decide import DecisionInputs, DecisionResult, decide
from matsya_intelligence.engine.core import EngineInputs, EngineResult, run_engine
from matsya_intelligence.io.adapters import as_of_cutoff, track_b_inputs
from matsya_intelligence.scenarios import breaking_point, preset, presets, run_scenario
from matsya_intelligence.scenarios.run import BreakingPoint, ScenarioResult
from matsya_intelligence.timing import TimingModel, fit_timing

BREAKING_TARGET = 0.5  # a preset "breaks" the business when the 90-day chance reaches the runway rule


@dataclass
class TenantRun:
    tenant_id: str
    as_of: date
    run_id: str  # the baseline forecast run
    inputs: EngineInputs
    baseline: EngineResult
    plan: DecisionResult
    scenarios: list[tuple[str, dict, ScenarioResult, BreakingPoint | None]]  # (preset key, preset entry, ...)
    attribution: AttributionResult
    timing: TimingModel
    invoices: pl.DataFrame  # adapter invoices (as_of read)
    outcomes: pl.DataFrame  # adapter invoice outcomes (as_of read)
    buyers: pl.DataFrame  # adapter buyers, with names
    calibrator: Calibrator | None
    data_through: date  # ledger complete through (watermark)
    latest_known_at: datetime | None  # newest transaction known by as_of
    extra: dict = field(default_factory=dict)


def compute_tenant_run(tables: dict[str, pl.DataFrame], inp: EngineInputs, dinp: DecisionInputs, run_id: str,
                       n_paths: int = 10_000, calibrator: Calibrator | None = None,
                       preset_names: list[str] | None = None, plan_method: str | None = None) -> TenantRun:
    """inp, dinp: the engine and decision inputs (build_learned_inputs, decision_inputs) for this tenant.
    run_id: the base id; the baseline forecast is `<run_id>:decide:verify`."""
    as_of = inp.as_of
    plan = decide(inp, dinp, run_id, n_verify=n_paths, method=plan_method)
    base_id = plan.record["seed_ids"]["verify"]
    baseline = run_engine(inp, base_id, n_paths, 90, keep_flows=True)
    if calibrator is None:
        calibrator = Calibrator(horizons=PRODUCTION_HORIZONS)
    scen = []
    for name in preset_names or list(presets()):
        entry = presets()[name]
        ts = preset(name, as_of)
        sr = run_scenario(inp, base_id, ts, name=name, n_paths=n_paths, horizon=90, base=baseline)
        bp = breaking_point(inp, base_id, ts, 0, BREAKING_TARGET, day=90, n_paths=n_paths, horizon=90) if ts else None
        scen.append((name, entry, sr, bp))
    attr = attribute(inp, run_id, n_paths=n_paths)
    obs = track_b_inputs(tables, as_of)
    tm = fit_timing(obs["invoice_outcomes"], obs["invoices"], obs["buyers"], obs["complete_through"], run_id=run_id)
    txns = tables["ledger.transactions"]
    known = txns.filter(pl.col("known_at") <= as_of_cutoff(as_of))["known_at"]
    return TenantRun(tenant_id=inp.tenant_id, as_of=as_of, run_id=base_id, inputs=inp, baseline=baseline, plan=plan,
                     scenarios=scen, attribution=attr, timing=tm, invoices=obs["invoices"],
                     outcomes=obs["invoice_outcomes"], buyers=obs["buyers"], calibrator=calibrator,
                     data_through=obs["complete_through"], latest_known_at=known.max() if known.len() else None)


def p_short_by_day(res: EngineResult) -> np.ndarray:
    """Chance of having been below the threshold by each day 1..H."""
    return res.shortfall["p_short"].to_numpy()
