"""Decision engine: the cheapest set of actions that keeps the chance of a shortfall by day D at or
below epsilon, as a sample-average MILP solved with HiGHS, then checked on fresh paths.

Formulation, on N subsample paths (seed `<run>:decide`), balance B[n, t] for days t = 1..D:

  minimise   sum_a cost_a x_a
  subject to B[n, t] + sum_v delta_v[n, t] x_v + M[n, t] z_n >= theta   every kept (n, t)
             sum_n z_n <= floor(f eps N)    (f = solve_eps_factor, 0.8: the in-sample count is
                                             optimistic, so the solve aims below eps)
             at most one action per invoice, one stretch length per supplier, overdraft units <= limit
             y_{c,i} = x_c (1 - sum x_{a on i})   (linearised: a chased invoice that is also discounted
                                                    or claimed loses its chase effect)
  x binary (overdraft: integer units), z binary, y in [0, 1].

delta_v is the change in path n's balance on day t from variable v on the baseline's random numbers
(actions.py). Effects add, except the interaction handled by y; GST on stretched purchases and a
claim on a chased invoice are the remaining approximations, which the fresh-path check covers.

Row pruning. lo[n, t] = B + sum_v min(0, ub_v delta_v): a row with lo >= theta holds for every plan
and is dropped; M[n, t] = theta - lo is the tightest valid big-M. A path with
B + sum_v max(0, ub_v delta_v) < theta on some day cannot be saved: z_n is fixed to 1.

MSMED claims are not plan actions: their cash lands on the claim day (day 90), so they cannot
lower p_short, and including them only mixed their expected value into the plan's cost. They are
reported as a separate list of money the tenant is owed, with the net value of claiming each.

If no plan reaches eps, a first MILP minimises sum z (the smallest achievable shortfall share) and a
second finds the cheapest plan at that share; the result says the target was not met.

Verification: the chosen transforms run on N_verify fresh paths (seed `<run>:verify`) against the
same-seed baseline (common random numbers). The baselines (do nothing, discount every eligible
invoice, chase the largest buyer) run on the same fresh paths.
"""

import json
import math
import time
from dataclasses import dataclass

import numpy as np
import polars as pl
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix, csr_matrix

from matsya_intelligence.decide import costs as C
from matsya_intelligence.decide.actions import (Action, DecisionInputs, candidate_actions, chase_invoice_delta,
                                                invoice_delta, owed_interest)
from matsya_intelligence.engine.core import EngineInputs, EngineResult, run_engine
from matsya_intelligence.scenarios.apply import apply_transforms
from matsya_intelligence.scenarios.transforms import to_json, transforms_hash

SCALE = 100_000  # MILP works in Rs 1,000 units for numerics; reported figures stay in paise
MODEL_VERSION = "saa-milp-highs-v1"


@dataclass
class Columns:
    """The MILP's variables on one set of paths: actions, then y (chase x invoice) variables."""

    acts: list[Action]
    y: list[tuple[int, int]]  # (chase action index, open invoice index)
    base: EngineResult
    chase_runs: dict[int, EngineResult]
    stretch_runs: dict[int, EngineResult]
    inp: EngineInputs
    day: int

    @property
    def n_vars(self) -> int:
        return len(self.acts) + len(self.y)

    def ub(self, v: int) -> int:
        return self.acts[v].ub if v < len(self.acts) else 1

    def delta(self, v: int) -> np.ndarray:
        """(paths, day) int64 balance change per unit of variable v."""
        D, H = self.day, self.base.balance.shape[1] - 1
        if v >= len(self.acts):
            c, i = self.y[v - len(self.acts)]
            return chase_invoice_delta(self.base, self.chase_runs[c], self.inp, i, H)[:, :D]
        a = self.acts[v]
        if a.kind in ("treds_discount", "msmed_interest"):
            return invoice_delta(self.base, self.inp, a, H)[:, :D]
        if a.kind == "overdraft":
            n = self.base.balance.shape[0]
            return np.broadcast_to(np.where(np.arange(1, D + 1) >= a.detail["draw_day"], a.unit_paise, 0),
                                   (n, D)).astype(np.int64)
        if a.kind == "supplier_stretch":
            return (self.stretch_runs[v].balance - self.base.balance)[:, 1:D + 1]
        # chase: whole-run change minus the invoices that have their own y variable
        d = (self.chase_runs[v].balance - self.base.balance)[:, 1:D + 1]
        for c, i in self.y:
            if c == v:
                d = d - chase_invoice_delta(self.base, self.chase_runs[v], self.inp, i, H)[:, :D]
        return d


def _columns(inp: EngineInputs, active: list, acts: list[Action], base: EngineResult, seed_id: str, n: int,
             horizon: int, day: int, only: set[int] | None = None) -> Columns:
    """Scenario runs for chase and stretch actions (all, or the indices in only) on seed_id."""
    def run(a: Action) -> EngineResult:
        return run_engine(apply_transforms(inp, active + a.transforms()), f"{seed_id}:{a.kind}:{a.target_id}", n,
                          horizon, keep_flows=True, seed_id=seed_id)

    want = set(range(len(acts))) if only is None else only
    chase = {j: run(a) for j, a in enumerate(acts) if a.kind == "chase" and j in want}
    stretch = {j: run(a) for j, a in enumerate(acts) if a.kind == "supplier_stretch" and j in want}
    inv_acted = {a.invoice for j, a in enumerate(acts) if a.invoice is not None and j in want}
    oi = inp.open_invoices
    y = [(j, int(i)) for j in sorted(chase) for i in np.flatnonzero(oi.buyer == acts[j].buyer) if int(i) in inv_acted] \
        if C.constants()["chase"]["interaction"]["value"] else []
    return Columns(acts, y, base, chase, stretch, inp, day)


def _p_short(bal: np.ndarray, theta: int, day: int) -> float:
    return float((bal[:, 1:day + 1].min(axis=1) < theta).mean())


def _short_days(bal: np.ndarray, theta: int, day: int) -> float:
    return float((bal[:, 1:day + 1] < theta).sum(axis=1).mean())


def _path_rows(cols: Columns, theta: int):
    """Balance rows that some plan can push below theta: (B, lo, hopeless paths, row path, row day,
    action-column triplets (row, var, coef) in SCALE units). lo = B + sum_v min(0, ub_v delta_v)."""
    B = cols.base.balance[:, 1:cols.day + 1].astype(float)
    lo, hi = B.copy(), B.copy()
    for v in range(cols.n_vars):
        d = cols.delta(v).astype(float) * cols.ub(v)
        lo += np.minimum(d, 0)
        hi += np.maximum(d, 0)
    hopeless = (hi < theta).any(axis=1)
    return B, lo, hopeless, hi


def _triplets(cols: Columns, rn: np.ndarray, rt: np.ndarray):
    out = []
    for v in range(cols.n_vars):
        vals = cols.delta(v)[rn, rt].astype(float) / SCALE
        nz = np.flatnonzero(vals)
        out.append((nz, np.full(nz.size, v), vals[nz]))
    return out


def _side_rows(cols: Columns, width: int) -> tuple[list, list, list]:
    """At most one action per invoice, one stretch length per supplier, overdraft units <= limit, and the
    y (chase x invoice) linearisation; rows over `width` columns (actions and y come first)."""
    V = cols.n_vars
    side, s_lo, s_hi = [], [], []

    def row(coefs: dict[int, float], lo_: float, hi_: float):
        r = np.zeros(width)
        for j, c in coefs.items():
            r[j] += c
        side.append(r)
        s_lo.append(lo_)
        s_hi.append(hi_)

    acts = cols.acts
    by_inv: dict[int, list[int]] = {}
    for j, a in enumerate(acts):
        if a.invoice is not None:
            by_inv.setdefault(a.invoice, []).append(j)
    for js in by_inv.values():
        if len(js) > 1:
            row({j: 1 for j in js}, -np.inf, 1)
    by_sup: dict[str, list[int]] = {}
    for j, a in enumerate(acts):
        if a.supplier is not None:
            by_sup.setdefault(a.supplier, []).append(j)
    for js in by_sup.values():
        row({j: 1 for j in js}, -np.inf, 1)
    od = [j for j, a in enumerate(acts) if a.kind == "overdraft"]
    if od:
        row({j: 1 for j in od}, -np.inf, acts[od[0]].ub)
    for yi, (c, i) in enumerate(cols.y):
        yv = len(acts) + yi
        on_i = by_inv.get(i, [])
        row({yv: 1, c: -1}, -np.inf, 0)
        row({yv: 1, **{j: 1 for j in on_i}}, -np.inf, 1)
        row({yv: -1, c: 1, **{j: -1 for j in on_i}}, -np.inf, 0)
    assert V <= width
    return side, s_lo, s_hi


def _info(res) -> dict:
    gap = getattr(res, "mip_gap", None)
    return {"status": int(res.status), "message": res.message, "time_limited": int(res.status) == 1,
            "mip_gap": None if gap is None or not np.isfinite(gap) else float(gap)}


def _solve(cols: Columns, theta: int, eps: float, time_limit: float):
    """Chance-constrained plan. Returns (x per variable, sum z, solver info, target met, rows). eps is the
    in-sample target."""
    B, lo, hopeless, _ = _path_rows(cols, theta)
    n, V = B.shape[0], cols.n_vars
    keep = (lo < theta) & ~hopeless[:, None]
    rn, rt = np.nonzero(keep)
    R = len(rn)
    # variables: x (V), z (n)
    blocks = _triplets(cols, rn, rt)
    rows = np.concatenate([b[0] for b in blocks] + [np.arange(R)])
    cidx = np.concatenate([b[1] for b in blocks] + [V + rn])
    vals = np.concatenate([b[2] for b in blocks] + [(theta - lo[rn, rt]) / SCALE])
    A_paths = coo_matrix((vals, (rows, cidx)), shape=(R, V + n)).tocsr()
    lb_paths = (theta - B[rn, rt]) / SCALE
    side, s_lo, s_hi = _side_rows(cols, V + n)
    acts = cols.acts

    cap = math.floor(eps * n + 1e-9)
    z_lb = hopeless.astype(float)
    lb = np.concatenate([np.zeros(V), z_lb])
    ub = np.concatenate([[cols.ub(v) for v in range(V)], np.ones(n)])
    integrality = np.concatenate([[1] * len(acts), [0] * len(cols.y), np.ones(n)])
    cost = np.concatenate([[a.cost_paise / SCALE for a in acts], np.zeros(len(cols.y)), np.zeros(n)])
    base_cons = [LinearConstraint(A_paths, lb_paths, np.inf)] if R else []
    if side:
        base_cons.append(LinearConstraint(csr_matrix(np.array(side)), s_lo, s_hi))
    z_row = csr_matrix(np.concatenate([np.zeros(V), np.ones(n)])[None, :])
    opts = {"time_limit": time_limit, "disp": False, "presolve": True}

    def go(c, zcap):
        cons = base_cons + [LinearConstraint(z_row, -np.inf, zcap)]
        return milp(c, constraints=cons, integrality=integrality, bounds=Bounds(lb, ub), options=opts)

    met = True
    res = go(cost, cap) if hopeless.sum() <= cap else None
    if res is None or res.x is None:
        met = False
        zc = np.concatenate([np.zeros(V), np.ones(n)])
        r1 = go(zc, n)
        if r1.x is None:
            raise RuntimeError(f"decision MILP failed: {r1.message}")
        best = int(round(r1.x[V:].sum()))
        res = go(cost, best)
        if res.x is None:
            res = r1
    x = np.round(res.x[:V]).astype(np.int64)
    x[len(acts):] = np.clip(x[len(acts):], 0, 1)
    return x, int(round(res.x[V:].sum())), _info(res), met, R


def _solve_cvar(cols: Columns, theta: int, alpha: float, budget_paise: int, time_limit: float):
    """CVaR plan (Rockafellar-Uryasev). Loss per path L_n = max(0, theta - min_t balance), linear in x:

      minimise   sum_a cost_a x_a
      subject to u_n + eta + sum_v delta_v[n, t] x_v >= theta - B[n, t]   every kept (n, t)
                 eta + sum_n u_n / (alpha N) <= budget
                 u >= 0, eta >= 0, side rows as in the chance-constrained plan

    so eta + mean(u) / alpha >= CVaR_alpha(L) at the optimum (eta is the VaR; L >= 0 so eta >= 0 loses
    nothing). No per-path binaries: only the actions are integer. If the budget cannot be met, a first
    solve minimises the CVaR and a second finds the cheapest plan within 0.1% (or Re 1) of that minimum.
    Returns (x, in-sample CVaR in paise, solver info, budget met, rows)."""
    B, lo, _, _ = _path_rows(cols, theta)
    n, V = B.shape[0], cols.n_vars
    rn, rt = np.nonzero(lo < theta)
    R = len(rn)
    # variables: x (V), u (n), eta
    W = V + n + 1
    blocks = _triplets(cols, rn, rt)
    rows = np.concatenate([b[0] for b in blocks] + [np.arange(R), np.arange(R)])
    cidx = np.concatenate([b[1] for b in blocks] + [V + rn, np.full(R, V + n)])
    vals = np.concatenate([b[2] for b in blocks] + [np.ones(R), np.ones(R)])
    cons = [LinearConstraint(coo_matrix((vals, (rows, cidx)), shape=(R, W)).tocsr(),
                             (theta - B[rn, rt]) / SCALE, np.inf)] if R else []
    side, s_lo, s_hi = _side_rows(cols, W)
    if side:
        cons.append(LinearConstraint(csr_matrix(np.array(side)), s_lo, s_hi))
    cvar_row = np.concatenate([np.zeros(V), np.full(n, 1 / (alpha * n)), [1.0]])
    acts = cols.acts
    lb = np.zeros(W)
    ub = np.concatenate([[cols.ub(v) for v in range(V)], np.full(n + 1, np.inf)])
    integrality = np.concatenate([[1] * len(acts), [0] * len(cols.y), np.zeros(n + 1)])
    cost = np.concatenate([[a.cost_paise / SCALE for a in acts], np.zeros(len(cols.y)), np.zeros(n + 1)])
    opts = {"time_limit": time_limit, "disp": False, "presolve": True}

    def go(c, cap):
        return milp(c, constraints=cons + [LinearConstraint(csr_matrix(cvar_row[None, :]), -np.inf, cap)],
                    integrality=integrality, bounds=Bounds(lb, ub), options=opts)

    met = True
    res = go(cost, budget_paise / SCALE)
    if res.x is None:
        met = False
        r1 = go(cvar_row, np.inf)
        if r1.x is None:
            raise RuntimeError(f"decision MILP failed: {r1.message}")
        best = float(cvar_row @ r1.x)
        res = go(cost, best * 1.001 + 100 / SCALE)
        if res.x is None:
            res = r1
    x = np.round(res.x[:V]).astype(np.int64)
    x[len(acts):] = np.clip(x[len(acts):], 0, 1)
    return x, float(cvar_row @ res.x) * SCALE, _info(res), met, R


def shortfall_stats(bal: np.ndarray, theta: int, day: int, alpha: float = 0.05) -> dict:
    """p_short by day, expected depth below theta (over paths that breach, and over all paths) and the
    average depth in the worst alpha of paths (CVaR), from a (paths, 1 + horizon) balance array."""
    depth = np.maximum(0, theta - bal[:, 1:day + 1].min(axis=1)).astype(float)
    k = max(1, math.ceil(alpha * depth.size))
    short = depth > 0
    return {"p_short": float(short.mean()), "es_cond_paise": float(depth[short].mean()) if short.any() else 0.0,
            "es_uncond_paise": float(depth.mean()), "cvar_paise": float(np.sort(depth)[-k:].mean())}


@dataclass
class DecisionResult:
    run_id: str
    tenant_id: str
    plan: pl.DataFrame  # decisions.plans plus net_cost_paise (signed; claims can be negative)
    chosen: list[tuple[Action, int]]
    transforms: list
    target_met: bool
    eps: float
    day: int
    n_sub: int
    n_verify: int
    p_short_sub: float  # the plan on the subsample (in-sample)
    p_short_verified: float  # on fresh paths
    p_short_verified_se: float
    baselines: pl.DataFrame
    msmed_owed: pl.DataFrame
    money_owed: pl.DataFrame  # MSMED claims: amount owed and the net value of claiming, best first
    solver: dict  # status, message, time_limited, mip_gap of the final solve
    eps_solve: float
    message: str
    n_candidates: int
    n_rows: int
    record: dict
    method: str = "chance"  # "chance" (chance-constrained) or "cvar"
    cvar_alpha: float | None = None
    cvar_budget_paise: int | None = None
    cvar_sub_paise: float | None = None  # the plan's in-sample CVaR (cvar plans)
    fresh: dict | None = None  # shortfall_stats on the fresh paths: {"do_nothing": ..., "plan": ...}
    p_short_by_day: dict | None = None  # fresh paths, days 1..horizon: {"do_nothing": array, "plan": array}

    @property
    def cost_paise(self) -> int:
        return sum(a.cost(k) for a, k in self.chosen)

    def contract_tables(self) -> dict[str, pl.DataFrame]:
        from matsya_contracts.validate import schema

        return {"decisions.plans": self.plan.select(list(schema("decisions.plans")))}


def _money_owed(inp: EngineInputs, dinp: DecisionInputs, claims: list[Action]) -> pl.DataFrame:
    """One row per invoice with a possible MSMED claim, highest net value first."""
    buyer = dict(zip(dinp.invoices["invoice_id"], dinp.invoices["buyer_id"]))
    rows = [{"tenant_id": inp.tenant_id, "invoice_id": a.target_id, "buyer_id": buyer.get(a.target_id),
             "principal_paise": a.detail["principal_paise"], "interest_paise": a.detail["interest_paise"],
             "claim_day": a.detail["on_day"], "p_unpaid_by_claim_day": a.detail["p_unpaid_by_claim_day"],
             "value_after_tax_paise": a.detail["value_paise"], "penalty_paise": a.detail["penalty_paise"],
             "net_value_paise": -a.cost_paise} for a in claims]
    schema = {"tenant_id": pl.String, "invoice_id": pl.String, "buyer_id": pl.String, "principal_paise": pl.Int64,
              "interest_paise": pl.Int64, "claim_day": pl.Int64, "p_unpaid_by_claim_day": pl.Float64,
              "value_after_tax_paise": pl.Int64, "penalty_paise": pl.Int64, "net_value_paise": pl.Int64}
    return pl.DataFrame(rows, schema=schema).sort(["net_value_paise", "invoice_id"], descending=[True, False])


def _baseline_row(name: str, bal: np.ndarray, theta: int, day: int, cost: int, n_actions: int) -> dict:
    return {"baseline": name, "n_actions": n_actions, "cost_paise": cost, "p_short": _p_short(bal, theta, day),
            "expected_shortfall_days": _short_days(bal, theta, day)}


def decide(inp: EngineInputs, dinp: DecisionInputs, base_run_id: str, active_transforms: list | None = None,
           n_sub: int | None = None, n_verify: int | None = None, horizon: int = 90,
           constants_overrides: dict | None = None, method: str | None = None) -> DecisionResult:
    """The cheapest plan meeting the target for this tenant at as_of, verified on fresh paths.

    method: "chance" (p_short by day D <= eps) or "cvar" (CVaR_alpha of the day-D shortfall depth <=
    budget); None = decision.plan_method. Both use the same solve and fresh paths (common random numbers).
    constants_overrides: decision constants by dotted path, for this call only (sensitivity runs);
    kept in the run record for replay."""
    with C.overrides(constants_overrides):
        method = method or C.constants()["plan_method"]["value"]
        if method not in ("chance", "cvar"):
            raise ValueError(f"unknown plan method {method!r}")
        res = _decide(inp, dinp, base_run_id, active_transforms, n_sub, n_verify, horizon, method)
    res.record["constants_overrides"] = dict(sorted((constants_overrides or {}).items()))
    return res


def _decide(inp: EngineInputs, dinp: DecisionInputs, base_run_id: str, active_transforms: list | None,
            n_sub: int | None, n_verify: int | None, horizon: int, method: str = "chance") -> DecisionResult:
    k = C.constants()
    active = list(active_transforms or [])
    eps = dinp.eps if dinp.eps is not None else k["shortfall_target_eps"]["value"]
    day = dinp.day if dinp.day is not None else k["shortfall_day"]["value"]
    n_sub = n_sub or k["milp_path_subsample"]["value"]
    n_verify = n_verify or k["verify_paths"]["value"]
    if not 1 <= day <= horizon:
        raise ValueError("shortfall day outside the horizon")
    theta = inp.threshold_paise
    if theta < 0:
        raise ValueError("the shortfall threshold is the owner cushion and cannot be negative; overdraft "
                         "headroom enters only as overdraft draws, so a negative threshold would count it twice")
    inp_a = apply_transforms(inp, active)
    seed_run = f"{base_run_id}:decide"  # both plan methods solve and verify on the same paths
    run_id = seed_run if method == "chance" else f"{seed_run}:cvar"
    sub_id, ver_id = f"{seed_run}:subsample", f"{seed_run}:verify"

    base = run_engine(inp_a, sub_id, n_sub, horizon, keep_flows=True)
    every = candidate_actions(inp_a, dinp, base, horizon, active)
    acts = [a for a in every if a.kind != "msmed_interest"]
    money_owed = _money_owed(inp_a, dinp, [a for a in every if a.kind == "msmed_interest"])
    cols = _columns(inp, active, acts, base, sub_id, n_sub, horizon, day)
    eps_solve = k["solve_eps_factor"]["value"] * eps
    alpha, budget = float(k["cvar"]["alpha"]["value"]), int(k["cvar"]["budget_paise"]["value"])
    t0 = time.perf_counter()
    if method == "chance":
        x, z_sum, solver, met, n_rows = _solve(cols, theta, eps_solve, k["milp_time_limit_s"]["value"])
        cvar_sub = None
    else:
        x, cvar_sub, solver, met, n_rows = _solve_cvar(cols, theta, alpha, budget, k["milp_time_limit_s"]["value"])
    solver["seconds"] = time.perf_counter() - t0
    chosen_idx = [j for j in range(len(acts)) if x[j] > 0]

    # In-sample plan balance (linear model) and leave-one-out importance for the ranking.
    B = base.balance[:, 1:day + 1].astype(np.int64)
    contrib = {v: cols.delta(v) * int(x[v]) for v in range(cols.n_vars) if x[v] > 0}
    plan_bal = B + sum(contrib.values()) if contrib else B
    p_plan_sub = float((plan_bal.min(axis=1) < theta).mean())

    def loo(j: int) -> tuple[float, float]:
        """Rise in p_short and in the in-sample CVaR of the depth when action j is dropped."""
        d = plan_bal - contrib[j]
        for yi, (c, _) in enumerate(cols.y):
            if c == j and len(acts) + yi in contrib:
                d = d - contrib[len(acts) + yi]
        cv = shortfall_stats(np.hstack([np.zeros((d.shape[0], 1)), d]), theta, day, alpha)["cvar_paise"]
        return float((d.min(axis=1) < theta).mean()) - p_plan_sub, cv

    imp = {j: loo(j) for j in chosen_idx}
    order = sorted(chosen_idx, key=lambda j: (-imp[j][0], -imp[j][1], acts[j].cost(int(x[j])), acts[j].kind,
                                              acts[j].target_id))

    # Fresh paths: baseline, the plan step by step (additive on these paths), the full plan as a run.
    vbase = run_engine(inp_a, ver_id, n_verify, horizon, keep_flows=True)
    vcols = _columns(inp, active, acts, vbase, ver_id, n_verify, horizon, day, only=set(chosen_idx))
    plan_ts = [t for j in order for t in acts[j].transforms(int(x[j]))]
    vplan = run_engine(apply_transforms(inp, active + plan_ts), f"{ver_id}:plan", n_verify, horizon, seed_id=ver_id)
    p_ver = _p_short(vplan.balance, theta, day)

    # A chased invoice that the plan also discounts or claims keeps no chase effect (its y is 0 in the
    # full plan), so the steps leave it out even before that invoice's own action is added.
    rows, running = [], vbase.balance[:, 1:day + 1].astype(np.int64)
    for rank, j in enumerate(order, start=1):
        running = running + vcols.delta(j) * int(x[j])
        p_after = float((running.min(axis=1) < theta).mean()) if rank < len(order) else p_ver
        a = acts[j]
        rows.append({"tenant_id": inp.tenant_id, "run_id": run_id, "rank": rank, "action": a.kind,
                     "target_id": a.target_id if a.kind != "overdraft" else f"{a.target_id}:{int(x[j]) * a.unit_paise}",
                     "cost_paise": max(a.cost(int(x[j])), 0), "p_short_after": p_after,
                     "net_cost_paise": a.cost(int(x[j]))})
    from matsya_contracts.validate import schema
    plan = pl.DataFrame(rows, schema={**schema("decisions.plans"), "net_cost_paise": pl.Int64})

    # Baselines on the same fresh paths.
    treds = [a for a in acts if a.kind == "treds_discount"]
    chases = [a for a in acts if a.kind == "chase"]
    base_rows = [_baseline_row("do_nothing", vbase.balance, theta, day, 0, 0)]
    if treds:
        r = run_engine(apply_transforms(inp, active + [t for a in treds for t in a.transforms()]),
                       f"{ver_id}:discount_all", n_verify, horizon, seed_id=ver_id)
        base_rows.append(_baseline_row("discount_all_eligible", r.balance, theta, day,
                                       sum(a.cost() for a in treds), len(treds)))
    if chases:
        big = max(chases, key=lambda a: (a.detail["open_paise"], a.target_id))
        r = run_engine(apply_transforms(inp, active + big.transforms()), f"{ver_id}:chase_largest", n_verify, horizon,
                       seed_id=ver_id)
        base_rows.append(_baseline_row("chase_largest_buyer", r.balance, theta, day, big.cost(), 1))
    base_rows.append(_baseline_row("plan", vplan.balance, theta, day,
                                   sum(acts[j].cost(int(x[j])) for j in chosen_idx), len(chosen_idx)))
    baselines = pl.DataFrame(base_rows)
    fresh = {"do_nothing": shortfall_stats(vbase.balance, theta, day, alpha),
             "plan": shortfall_stats(vplan.balance, theta, day, alpha)}
    by_day = {name: np.minimum.accumulate(r.balance[:, 1:], axis=1).__lt__(theta).mean(axis=0)
              for name, r in (("do_nothing", vbase), ("plan", vplan))}

    if method == "cvar":
        msg = (f"CVaR budget {'met' if met else 'cannot be met'} in sample: average shortfall in the worst "
               f"{alpha:.0%} of solve paths Rs {cvar_sub / 100:,.0f} (budget Rs {budget / 100:,.0f})"
               + ("" if met else "; plan minimises it"))
    elif met:
        msg = (f"target met in sample: {z_sum}/{n_sub} paths short by day {day} "
               f"(cap {math.floor(eps_solve * n_sub + 1e-9)} at {eps_solve:.3f}, target {eps:.3f})")
    else:
        msg = (f"target {eps:.3f} cannot be met with the available actions; plan minimises p_short "
               f"({z_sum}/{n_sub} paths short in sample)")
    record = {"run_id": run_id, "base_run_id": base_run_id, "tenant_id": inp.tenant_id, "as_of": inp.as_of.isoformat(),
              "model_version": MODEL_VERSION, "model_versions": inp.model_versions,
              "input_snapshot_ids": inp.input_snapshot_ids, "seed_ids": {"subsample": sub_id, "verify": ver_id},
              "active_transforms_json": to_json(active), "plan_transforms_json": to_json(plan_ts),
              "plan_transforms_hash": transforms_hash(plan_ts), "eps": eps, "day": day, "threshold_paise": theta,
              "n_sub": n_sub, "n_verify": n_verify, "eps_solve": eps_solve, "solver": solver["message"],
              "solver_status": solver["status"], "time_limited": solver["time_limited"], "mip_gap": solver["mip_gap"],
              "method": method,  # solve time is in DecisionResult.solver only: the record must replay exactly
              **({"cvar_alpha": alpha, "cvar_budget_paise": budget, "cvar_sub_paise": cvar_sub} if method == "cvar" else {})}
    return DecisionResult(
        run_id=run_id, tenant_id=inp.tenant_id, plan=plan, chosen=[(acts[j], int(x[j])) for j in order],
        transforms=plan_ts, target_met=met, eps=eps, day=day, n_sub=n_sub, n_verify=n_verify,
        p_short_sub=p_plan_sub, p_short_verified=p_ver,
        p_short_verified_se=math.sqrt(max(p_ver * (1 - p_ver), 1e-12) / n_verify),
        baselines=baselines, msmed_owed=owed_interest(inp_a, dinp), money_owed=money_owed, solver=solver,
        eps_solve=eps_solve, message=msg, n_candidates=len(acts),
        n_rows=n_rows, record=json.loads(json.dumps(record, default=str)), method=method,
        cvar_alpha=alpha if method == "cvar" else None, cvar_budget_paise=budget if method == "cvar" else None,
        cvar_sub_paise=cvar_sub, fresh=fresh, p_short_by_day=by_day)
