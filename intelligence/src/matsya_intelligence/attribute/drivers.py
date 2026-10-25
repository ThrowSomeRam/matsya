"""Attribution: which open invoices and which buyers drive the shortfall risk.

Simple counterfactuals on common random numbers: neutralise one driver at a time, keep every
random draw the same, and measure how much risk goes away.

- Invoice driver: the open invoice is paid in full on its due date (day 1 if it is already
  overdue; outside the window if the due date is beyond the horizon). Every other draw is
  unchanged, so the neutralised balance is the baseline balance plus that one invoice's change
  in cash on each path, computed exactly from the baseline's payment days (no re-run).
- Buyer driver: the buyer's weekly payment chance is scaled to its group's typical chance, the
  median posterior-mean p of the buyers in its sector (all buyers if it is alone there), for
  every invoice and every future week, by a rate multiplier typical / own. Re-run on the
  baseline's seed; the engine's per-row uniforms keep every other buyer's payments unchanged.
  The never-paid share is left as it is.

Contribution of a driver at day t = baseline minus neutralised, for
  p_short(t)       share of paths below the threshold on some day up to t;
  ES(t)            mean over all paths of the depth of the worst dip below the threshold up to t
                   (the unconditional expected shortfall, paise).
A positive contribution means the driver adds risk. Drivers are ranked by the contribution to
p_short at the last reported day, then ES, then id; the top `top` are kept. decisions.drivers
holds delta_p_short at that day; the 30/60-day and ES columns are in the result only, waiting
for an additive contract change.

Exact for invoices when unscheduled flows do not react to the balance (the default bootstrap);
with the quantile flow forecaster the invoice effect is approximate, as in the decision engine.
"""

import time
from dataclasses import dataclass

import numpy as np
import polars as pl

from matsya_intelligence.engine.core import EngineInputs, EngineResult, run_engine
from matsya_intelligence.scenarios.apply import apply_transforms
from matsya_intelligence.scenarios.transforms import RateMultiplier

DAYS = (30, 60, 90)
TOP = 10
MAX_FACTOR = 50.0  # a buyer with a near-zero chance would otherwise get an unbounded multiplier


@dataclass
class AttributionResult:
    run_id: str
    tenant_id: str
    table: pl.DataFrame  # every driver: kind, id, delta_p_short_<t>, delta_es_<t>_paise, rank
    drivers: pl.DataFrame  # decisions.drivers (top drivers)
    baseline: dict
    seconds: float

    def contract_tables(self) -> dict[str, pl.DataFrame]:
        return {"decisions.drivers": self.drivers}


def _risk(balance: np.ndarray, threshold: int, days: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    """(p_short at each day, unconditional expected shortfall at each day) from (paths, H + 1) balances."""
    running_min = np.minimum.accumulate(balance[:, 1:], axis=1)
    depth = np.maximum(threshold - running_min, 0)
    idx = np.array(days) - 1
    return (depth[:, idx] > 0).mean(axis=0), depth[:, idx].mean(axis=0)


def _invoice_delta(base: EngineResult, inp: EngineInputs, i: int, horizon: int) -> np.ndarray:
    """(paths, H + 1) change in balance when open invoice i is paid on its due date instead."""
    pay = base.open_pay[:, i]
    cash = int(inp.open_invoices.cash[i])
    days = np.arange(horizon + 1)[None, :]
    old = np.where((pay[:, None] >= 1) & (days >= pay[:, None]), cash, 0)
    due = int(max(inp.open_invoices.due_day[i], 1))
    new = np.where(days >= due, cash, 0) if due <= horizon else np.zeros_like(days)
    return (new - old).astype(np.int64)


def typical_chances(inp: EngineInputs) -> np.ndarray:
    """Per buyer: the median posterior-mean p of the buyers in its sector (all buyers if alone)."""
    own = np.array([float(np.mean(p)) for p in inp.timing.p])
    sectors = np.array(inp.buyer_sectors) if inp.buyer_sectors else np.zeros(len(own), dtype=object)
    out = np.empty(len(own))
    for b in range(len(own)):
        peers = np.flatnonzero(sectors == sectors[b])
        out[b] = np.median(own[peers if peers.size > 1 else np.arange(len(own))])
    return out


def attribute(inp: EngineInputs, run_id: str, n_paths: int = 10_000, horizon: int = 90,
              days: tuple[int, ...] = DAYS, top: int = TOP) -> AttributionResult:
    """Contributions of every open invoice and every buyer to shortfall risk, on run_id's seed."""
    t0 = time.perf_counter()
    if inp.open_invoices.due_day is None and len(inp.open_invoices.buyer):
        raise ValueError("open invoices need due_day (build_learned_inputs sets it)")
    theta = inp.threshold_paise
    run = f"{run_id}:attribute"
    base = run_engine(inp, run, n_paths, horizon, keep_flows=True)
    p0, es0 = _risk(base.balance, theta, days)
    rows = []
    oi = inp.open_invoices
    ids = inp.open_invoice_ids or [f"open-{i}" for i in range(len(oi.buyer))]
    for i in range(len(oi.buyer)):
        p, es = _risk(base.balance + _invoice_delta(base, inp, i, horizon), theta, days)
        rows.append(("invoice", ids[i], p0 - p, es0 - es))
    own = np.array([float(np.mean(p)) for p in inp.timing.p])
    typical = typical_chances(inp)
    for b, buyer in enumerate(inp.buyer_ids):
        if not (oi.buyer == b).any() and inp.sales is None:
            continue  # no invoices of this buyer can be paid in the window
        factor = float(min(typical[b] / max(own[b], 1e-9), MAX_FACTOR))
        res = run_engine(apply_transforms(inp, [RateMultiplier(target="buyer", ids=[buyer], factor=factor)]),
                         f"{run}:buyer:{buyer}", n_paths, horizon, seed_id=run)
        p, es = _risk(res.balance, theta, days)
        rows.append(("buyer", buyer, p0 - p, es0 - es))
    last = days.index(max(days))
    data = {"driver_kind": [r[0] for r in rows], "driver_id": [r[1] for r in rows]}
    for j, d in enumerate(days):
        data[f"delta_p_short_{d}"] = [float(r[2][j]) for r in rows]
        data[f"delta_es_{d}_paise"] = [int(round(r[3][j])) for r in rows]
    table = pl.DataFrame(data, schema={"driver_kind": pl.String, "driver_id": pl.String,
                                       **{f"delta_p_short_{d}": pl.Float64 for d in days},
                                       **{f"delta_es_{d}_paise": pl.Int64 for d in days}})
    d_last = max(days)
    table = table.sort([f"delta_p_short_{d_last}", f"delta_es_{d_last}_paise", "driver_kind", "driver_id"],
                       descending=[True, True, False, False]).with_row_index("rank", offset=1)
    from matsya_contracts.validate import schema

    drivers = table.head(top).select(
        pl.lit(inp.tenant_id).alias("tenant_id"), pl.lit(run).alias("run_id"), "driver_kind", "driver_id",
        pl.col(f"delta_p_short_{d_last}").clip(-1.0, 1.0).alias("delta_p_short")).cast(schema("decisions.drivers"))
    baseline = {f"p_short_{d}": float(p0[j]) for j, d in enumerate(days)} | \
               {f"es_{d}_paise": int(round(es0[j])) for j, d in enumerate(days)} | {"n_paths": n_paths, "last": last}
    return AttributionResult(run_id=run, tenant_id=inp.tenant_id, table=table, drivers=drivers, baseline=baseline,
                             seconds=time.perf_counter() - t0)
