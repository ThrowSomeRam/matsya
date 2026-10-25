"""Run a scenario against its baseline on common random numbers, and search for breaking points.

run_scenario(inp, base_run_id, transforms) runs the baseline (or takes it) and the scenario
with seed_id = base_run_id, so every random stream is shared and the difference is the
scenario. Outputs, by day 1..H:
  d_p_short             p_short(scenario) - p_short(baseline)
  d_es_cond_paise       change in expected shortfall over paths short by that day
  d_es_uncond_paise     change in p_short x ES (the unconditional expected depth)
and runway_lost_days = baseline runway - scenario runway (a runway beyond the horizon counts
as H + 1, so the number is a lower bound when the baseline runway is beyond the horizon).

Breaking point: for one transform, the smallest change in its magnitude, from a benign value
towards a severe one, at which p_short(day) reaches the owner's target. A grid of runs on the
same seed first checks that p_short rises with the magnitude (reported if it does not: CRN
keeps the streams but not the alignment of draws inside a stream when counts change) and
brackets the first crossing; bisection then narrows it to tol.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from matsya_intelligence.engine.core import EngineInputs, EngineResult, run_engine
from matsya_intelligence.scenarios.apply import apply_transforms
from matsya_intelligence.scenarios.transforms import MAGNITUDE, TRANSFORM_LIST, to_json, transforms_hash, with_magnitude

PRESETS_PATH = Path(__file__).resolve().parents[3] / "config" / "scenario_presets.yaml"

EFFECTS_SCHEMA = {"tenant_id": pl.String, "scenario_id": pl.String, "base_run_id": pl.String, "run_id": pl.String,
                  "date": pl.Date, "day": pl.Int32, "p_short_base": pl.Float64, "p_short": pl.Float64,
                  "d_p_short": pl.Float64, "d_es_cond_paise": pl.Int64, "d_es_uncond_paise": pl.Int64}


# ---------------------------------------------------------------- presets


def presets() -> dict:
    return yaml.safe_load(PRESETS_PATH.read_text())


def _resolve(x, as_of: date):
    if isinstance(x, str) and x.startswith("+") and x.endswith("d"):
        return (as_of + timedelta(days=int(x[1:-1]))).isoformat()
    if isinstance(x, dict):
        return {k: _resolve(v, as_of) for k, v in x.items()}
    if isinstance(x, list):
        return [_resolve(v, as_of) for v in x]
    return x


def preset(name: str, as_of: date) -> list:
    """The preset's transform list with its relative dates resolved at as_of."""
    return TRANSFORM_LIST.validate_python(_resolve(presets()[name]["transforms"], as_of))


def preset_view_days(name: str) -> list[int]:
    """Days a preset is reported at (its run horizon is the last); default [90]."""
    return sorted(presets()[name].get("view_days", [90]))


# ---------------------------------------------------------------- one scenario


@dataclass
class BreakingPoint:
    transform_type: str
    field: str
    target: float
    day: int
    magnitude: float | None  # None: the severe end does not reach the target
    p_short_at: float | None
    grid: list[tuple[float, float]]  # (magnitude, p_short(day)) on the monotonicity grid
    monotone: bool
    violations: list[tuple[float, float, float, float]] = field(default_factory=list)  # (m1, p1, m2, p2) drops
    evaluations: int = 0


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    transforms: list
    base: EngineResult
    result: EngineResult
    effects: pl.DataFrame  # per day, EFFECTS_SCHEMA (no contract table yet, see track-b-decisions)
    runway_lost_days: int
    runway_lost_is_lower_bound: bool
    breaking: BreakingPoint | None = None

    def scenario_row(self) -> pl.DataFrame:
        """forecast.scenarios: params_json holds the full transform list, its hash and the baseline."""
        from matsya_contracts.validate import schema

        params = {"transforms": json.loads(to_json(self.transforms)), "transforms_hash": transforms_hash(self.transforms),
                  "base_run_id": self.base.run["run_id"], "run_id": self.result.run["run_id"],
                  "runway_lost_days": self.runway_lost_days,
                  "runway_lost_is_lower_bound": self.runway_lost_is_lower_bound}
        if self.breaking is not None:
            b = self.breaking
            params["breaking_point"] = {"field": b.field, "target": b.target, "day": b.day, "monotone": b.monotone,
                                        "grid": b.grid, "p_short_at": b.p_short_at}
        return pl.DataFrame([{"tenant_id": self.result.run["tenant_id"], "scenario_id": self.scenario_id,
                              "name": self.name, "params_json": json.dumps(params, sort_keys=True),
                              "breaking_magnitude": None if self.breaking is None else self.breaking.magnitude}],
                            schema=schema("forecast.scenarios"))

    def at_days(self, days: list[int]) -> pl.DataFrame:
        """p_short and expected shortfall (conditional and unconditional), baseline and scenario, at each day."""
        cols = ["day", "p_short", "expected_shortfall_cond_paise", "expected_shortfall_uncond_paise"]
        b = self.base.shortfall.select(cols).rename({c: f"{c}_base" for c in cols[1:]})
        s = self.result.shortfall.select(cols)
        return b.join(s, on="day").filter(pl.col("day").is_in(days)).sort("day")

    def contract_tables(self) -> dict[str, pl.DataFrame]:
        """forecast.scenarios plus the scenario run's forecast.runs / balance_quantiles / shortfall rows."""
        return {"forecast.scenarios": self.scenario_row(), **self.result.contract_tables()}


def scenario_id_for(transforms: list, name: str = "") -> str:
    return "SC-" + hashlib.sha256(f"{name}:{to_json(transforms)}".encode()).hexdigest()[:12]


def _runway(res: EngineResult, H: int) -> int:
    return res.run["runway_days"] if res.run["runway_days"] is not None else H + 1


def scenario_run(inp: EngineInputs, base_run_id: str, transforms: list, scenario_id: str, n_paths: int,
                 horizon: int, keep_flows: bool = False) -> EngineResult:
    res = run_engine(apply_transforms(inp, transforms), f"{base_run_id}:scenario:{scenario_id}", n_paths, horizon,
                     keep_flows=keep_flows, seed_id=base_run_id)
    # The full list is the replay record. forecast.runs.overrides_json only holds contract
    # OverrideItems (one-off events) until the contract widens; forecast.scenarios carries it.
    res.run["scenario_id"] = scenario_id
    res.run["transforms_json"] = to_json(transforms)
    res.run["transforms_hash"] = transforms_hash(transforms)
    return res


def run_scenario(inp: EngineInputs, base_run_id: str, transforms: list, name: str = "owner edit",
                 n_paths: int = 10_000, horizon: int = 90, base: EngineResult | None = None,
                 scenario_id: str | None = None, keep_flows: bool = False) -> ScenarioResult:
    if base is None:
        base = run_engine(inp, base_run_id, n_paths, horizon, keep_flows=keep_flows)
    if base.run["run_id"] != base_run_id or base.run["n_paths"] != n_paths:
        raise ValueError("baseline must be the run base_run_id with the same n_paths")
    sid = scenario_id or scenario_id_for(transforms, name)
    res = scenario_run(inp, base_run_id, transforms, sid, n_paths, horizon, keep_flows)
    b, s = base.shortfall, res.shortfall
    effects = pl.DataFrame({
        "tenant_id": inp.tenant_id, "scenario_id": sid, "base_run_id": base_run_id, "run_id": res.run["run_id"],
        "date": s["date"], "day": s["day"], "p_short_base": b["p_short"], "p_short": s["p_short"],
        "d_p_short": s["p_short"] - b["p_short"],
        "d_es_cond_paise": s["expected_shortfall_cond_paise"] - b["expected_shortfall_cond_paise"],
        "d_es_uncond_paise": s["expected_shortfall_uncond_paise"] - b["expected_shortfall_uncond_paise"],
    }, schema=EFFECTS_SCHEMA)
    return ScenarioResult(scenario_id=sid, name=name, transforms=list(transforms), base=base, result=res,
                          effects=effects, runway_lost_days=_runway(base, horizon) - _runway(res, horizon),
                          runway_lost_is_lower_bound=base.run["runway_days"] is None)


# ---------------------------------------------------------------- breaking point


def default_range(t, inp: EngineInputs) -> tuple[float, float]:
    """(benign, severe) magnitudes for a transform type; pass your own for anything else."""
    m = getattr(t, MAGNITUDE[t.type])
    if t.type in ("rate_multiplier",):
        return 1.0, 0.0
    if t.type == "freeze":
        return 0.0, 1.0
    if t.type == "cash_event":
        scale = abs(m) or abs(inp.opening_balance_paise) or 1
        return 0.0, -10.0 * scale if m <= 0 else 10.0 * scale
    if t.type == "terms_change":  # buyers: longer terms hurt; suppliers: shorter terms hurt
        if t.party == "supplier":
            return float(t.from_days), 0.0
        return float(t.from_days if t.from_days is not None else 0), 4.0 * max(m, 1)
    return 0.0, 4.0 * m  # date_shift


def breaking_point(inp: EngineInputs, base_run_id: str, transforms: list, index: int, target: float,
                   day: int = 90, benign: float | None = None, severe: float | None = None, tol: float | None = None,
                   n_paths: int = 10_000, horizon: int = 90, grid: int = 9, max_iter: int = 40) -> BreakingPoint:
    """Smallest move of transforms[index]'s magnitude from benign towards severe with p_short(day) >= target.

    Other transforms in the list are held fixed. tol defaults to 1/1000 of the range (1 for integer fields).
    """
    t = transforms[index]
    lo_d, hi_d = default_range(t, inp)
    benign = lo_d if benign is None else benign
    severe = hi_d if severe is None else severe
    is_int = not isinstance(getattr(t, MAGNITUDE[t.type]), float)
    tol = tol if tol is not None else (1.0 if is_int else abs(severe - benign) / 1000)
    sid = scenario_id_for(transforms, f"breaking:{index}")
    n_eval = 0

    def p_at(m: float) -> float:
        nonlocal n_eval
        n_eval += 1
        ts = list(transforms)
        ts[index] = with_magnitude(t, m)
        res = scenario_run(inp, base_run_id, ts, sid, n_paths, horizon)
        return float(res.shortfall["p_short"][day - 1])

    ms = np.linspace(benign, severe, grid)
    if is_int:
        ms = np.unique(np.round(ms)) if severe >= benign else np.unique(np.round(ms))[::-1]
    ps = [p_at(float(m)) for m in ms]
    pts = list(zip(map(float, ms), ps))
    drops = [(m1, p1, m2, p2) for (m1, p1), (m2, p2) in zip(pts, pts[1:]) if p2 < p1]
    bp = BreakingPoint(t.type, MAGNITUDE[t.type], target, day, None, None, pts, not drops, drops)
    if ps[0] >= target:
        bp.magnitude, bp.p_short_at = float(ms[0]), ps[0]
    else:
        k = next((i for i, p in enumerate(ps) if p >= target), None)
        if k is not None:
            a, b, pb = float(ms[k - 1]), float(ms[k]), ps[k]
            for _ in range(max_iter):
                if abs(b - a) <= tol:
                    break
                mid = (a + b) / 2
                if is_int:
                    mid = float(round(mid))
                    if mid in (a, b):
                        break
                pm = p_at(mid)
                if pm >= target:
                    b, pb = mid, pm
                else:
                    a = mid
            bp.magnitude, bp.p_short_at = b, pb
    bp.evaluations = n_eval
    return bp
