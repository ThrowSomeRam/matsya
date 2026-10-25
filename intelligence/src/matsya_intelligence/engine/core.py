"""Simulation engine: daily balance paths over the horizon, and the forecast outputs.

Days are indexed relative to as_of: day 0 is as_of (its closing balance is the
opening balance), days 1..H are simulated. Every flow is int64 paise.

Each component draws from its own seeding stream derived from run_id, so a
change to one input (or an override) never reshuffles another component's
draws. That is what makes scenario comparisons use common random numbers.

Per path:
  1. one posterior draw per buyer (payment-timing model), used for all its invoices;
  2. sales come from the sales sampler (which applies any demand level per path);
  3. open invoices: dead with q = P(dead | survived to the cut), else a payment
     day from the two-phase weekly chances, the rest of the partly elapsed week
     first; future invoices: the same from age 0;
  4. linked purchases; GST per month from that path's sales and purchases;
  5. fixed obligations, unscheduled flows, overrides; cumulative balance.

Scenario effects (scenarios component) are applied here at run time, never to stored
tables. Their own randomness (cash event days in a window, extra orders) comes from a
separate "engine:scenario" stream, so the baseline streams are untouched: a scenario run
with the baseline's seed_id differs from the baseline only by the scenario.

Payment simulation draws a uniform for every row in every simulated week, active or not, so
row r's draws do not depend on whether other rows have been paid. A change to one buyer's
chances (or to one invoice) then moves only that buyer's (or invoice's) payments.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from matsya_intelligence.seeding import rng_for, seed_from_run_id
from matsya_intelligence.timing.empirical import J_HI, J_LO, N_BINS

STREAMS = ("timing_draws", "open_invoices", "sales", "sales_timing", "unscheduled", "shocks")


@dataclass(frozen=True)
class TimingDraws:
    """Posterior draws per buyer, index-aligned with EngineInputs.buyer_ids: arrays (n_draws,).

    p_early_base, p_base: base chances for future weeks under the shock hook, so that the
    shock-averaged chance equals the posterior (shocks.sector.base_chance). None = use p.
    Past weeks (survival, the elapsed part of the current week) always use the posterior.

    hazard[b] (timing.empirical, None for Beta-geometric buyers) replaces the two-phase chances
    in every week for that buyer; the never-paid share still comes from pi and the Beta-geometric
    survival, as in the payment-timing model's own predictions. Shock shifts and scenario
    multipliers apply to it the same way: logit shift, then x factor clipped to [0, 1].
    """

    p_early: list[np.ndarray]
    p: list[np.ndarray]
    pi: list[np.ndarray]
    p_early_base: list[np.ndarray] | None = None
    p_base: list[np.ndarray] | None = None
    hazard: list[np.ndarray | None] | None = None  # empirical mode: weekly hazard by bin of k - due week
    hazard_base: list[np.ndarray | None] | None = None  # its base chances under the shock hook


@dataclass(frozen=True)
class Invoices:
    """Invoices to be paid. issue_day relative to as_of; cash = gross - expected TDS."""

    buyer: np.ndarray  # int index into buyer_ids
    issue_day: np.ndarray
    due_week: np.ndarray  # terms_days // 7 + 1
    cash: np.ndarray  # int64 paise
    gross: np.ndarray  # int64 paise, for GST
    taxable: np.ndarray  # float paise, for GST
    path: np.ndarray | None = None  # per-path invoices (sales); None = every path (open invoices)


@dataclass(frozen=True)
class OneOffEvent:
    """Override: a single cash event. Positive = inflow."""

    on: date
    amount_paise: int
    kind: str = "one_off"


@dataclass(frozen=True)
class ScenarioEffects:
    """Run-time changes from the scenarios component that are not edits of the inputs themselves.

    pay_mult, due_week: per buyer (index-aligned with buyer_ids); they apply to weeks starting
    on day >= 1 only, so survival to the cut (the past) keeps the posterior and the old terms.
    due_week -1 = unchanged. order_mult scales the sales forecaster's order rate (purchases
    follow per invoice; when orders fall, purchases of dropped orders on days <= purchase lag +
    purchase_sticky_days are still paid). purchase_shift_days moves every future supplier payment (earliest day 1).
    cash: (first_day, last_day, amount_paise), the day drawn per path uniformly in the window.
    freezes: (start_day, days, fraction): that share of each path's balance on start_day - 1
    (if positive) is unusable from start_day for `days` days.
    """

    pay_mult: np.ndarray | None = None
    due_week: np.ndarray | None = None
    order_mult: float = 1.0
    purchase_sticky_days: int = 0
    purchase_shift_days: int = 0
    cash: tuple[tuple[int, int, int], ...] = ()
    freezes: tuple[tuple[int, int, float], ...] = ()
    # (buyer mask, factor, last_day): x factor for weeks starting on days 1..last_day
    pay_windows: tuple[tuple[np.ndarray, float, int], ...] = ()
    # (open invoice index, day, amount_paise, mode): mode "replace" pays the invoice on day with
    # amount instead of its timing; "if_unpaid" does so only on paths not paid by then
    invoice_cash: tuple[tuple[int, int, int, str], ...] = ()
    # (share, days): that share of every future supplier payment moves by days (earliest day 1)
    purchase_partial_shifts: tuple[tuple[float, int], ...] = ()


SalesSampler = Callable[..., tuple[Invoices, pl.DataFrame]]
# (n_paths, horizon, rng[, order_mult=, extra_rng=]) -> (future invoices with path set, purchases: path, day,
# amount_paise > 0). order_mult/extra_rng/purchase_sticky_days are passed only when a scenario changes the order rate.
UnscheduledSampler = Callable[[int, int, np.random.Generator], np.ndarray]
# (n_paths, horizon, rng) -> int64 (n_paths, horizon + 1) net flows, column 0 unused
ShiftFn = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]
# (path, buyer, week_start_day) -> logit shift; the sector shock hook


@dataclass
class EngineInputs:
    tenant_id: str
    as_of: date
    opening_balance_paise: int
    threshold_paise: int
    buyer_ids: list[str]
    timing: TimingDraws
    weekday_probs: np.ndarray  # (n_buyers, 7) payment weekday probabilities, Monday = 0
    open_invoices: Invoices
    survival_through_day: int  # last day (relative) through which open invoices are known unpaid
    sales: SalesSampler | None
    fixed: list[tuple[int, int]]  # (day, outflow amount > 0)
    gst_rate: float
    gst_due_days: list[int]  # days on which a GST payment falls (base month = previous month)
    gst_history_net: dict[int, float]  # month key -> taxable sales - taxable purchases already happened
    gst_carry_in: float  # carry into the earliest base month needed
    unscheduled: UnscheduledSampler | None
    shift: ShiftFn | None = None  # fixed shift (true-parameter mode); also used for survival
    shift_sampler: Callable[[int, int, np.random.Generator], ShiftFn] | None = None  # per-run shock paths
    model_versions: dict = field(default_factory=dict)
    input_snapshot_ids: list[str] = field(default_factory=list)
    features: dict[str, pl.DataFrame] = field(default_factory=dict)  # component outputs, contract tables
    buyer_sectors: list[str] = field(default_factory=list)  # index-aligned with buyer_ids
    fixed_kinds: list[str] = field(default_factory=list)  # index-aligned with fixed (obligation kind)
    fixed_ids: list[str] = field(default_factory=list)  # index-aligned with fixed (obligation_id)
    scenario: ScenarioEffects | None = None
    open_invoice_ids: list[str] = field(default_factory=list)  # index-aligned with open_invoices


def month_key(d: date) -> int:
    return d.year * 12 + d.month - 1


def _sigmoid_shift(p: np.ndarray, shift: np.ndarray | None) -> np.ndarray:
    if shift is None:
        return p
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return 1 / (1 + np.exp(-(np.log(p / (1 - p)) + shift)))


def _offset_probs(inp: EngineInputs, buyer: np.ndarray, issue_day: np.ndarray) -> np.ndarray:
    """(N, 7): probability of paying on offset j within a week since issue, from weekday patterns."""
    wd0 = (inp.as_of.weekday() + issue_day) % 7
    cols = (wd0[:, None] + np.arange(7)[None, :]) % 7
    return inp.weekday_probs[buyer[:, None], cols]


def _pick_offset(u: np.ndarray, probs: np.ndarray, start: np.ndarray) -> np.ndarray:
    """Offset j >= start, with probability proportional to probs[j]; u uniform per row."""
    w = np.where(np.arange(7)[None, :] >= start[:, None], probs, 0.0)
    cdf = np.cumsum(w, axis=1)
    u = u * cdf[:, -1]
    return (cdf < u[:, None]).sum(axis=1)


def _simulate_payments(inp: EngineInputs, rng: np.random.Generator, path: np.ndarray, inv_idx: np.ndarray,
                       inv: Invoices, draw_idx: np.ndarray, survive: np.ndarray, horizon: int,
                       shift_fn: ShiftFn | None) -> np.ndarray:
    """Payment day per (path, invoice) row, or -1 if dead or after the horizon.

    Uniforms are drawn for every row each week (see the module docstring).
    survive[row] = survival probability to the cut (1 for invoices with no elapsed time);
    elapsed weeks c and days r come from survival_through_day.
    """
    b = inv.buyer[inv_idx]
    issue = inv.issue_day[inv_idx]
    due = inv.due_week[inv_idx]
    d = draw_idx[path, b]
    pe = np.concatenate(inp.timing.p_early)[_flat_index(inp, b, d)]
    p = np.concatenate(inp.timing.p)[_flat_index(inp, b, d)]
    pi = np.concatenate(inp.timing.pi)[_flat_index(inp, b, d)]
    sc = inp.scenario
    mult = None if sc is None or sc.pay_mult is None else sc.pay_mult[b]
    windows = [(mask[b], factor, last) for mask, factor, last in (sc.pay_windows if sc is not None else ())]
    due_new = due if sc is None or sc.due_week is None else np.where(sc.due_week[b] >= 0, sc.due_week[b], due)
    based = shift_fn is not None and inp.timing.p_base is not None
    if based:
        pe_b = np.concatenate(inp.timing.p_early_base)[_flat_index(inp, b, d)]
        p_b = np.concatenate(inp.timing.p_base)[_flat_index(inp, b, d)]
    hz = hz_b = None
    if inp.timing.hazard is not None:
        hz = _hazard_matrix(inp.timing.hazard)
        hz_b = _hazard_matrix(inp.timing.hazard_base) if based and inp.timing.hazard_base is not None else hz
        emp = ~np.isnan(hz[b, 0])

    def chance(rows, week, due_w, early, late, h_mat):
        out = np.where(week < due_w, early[rows], late[rows])
        if h_mat is not None:
            out = np.where(emp[rows], h_mat[b[rows], np.clip(week - due_w, J_LO, J_HI) - J_LO], out)
        return out

    elapsed = np.maximum(0, inp.survival_through_day - issue + 1)
    c, r = elapsed // 7, elapsed % 7
    probs = _offset_probs(inp, b, issue)
    q = pi / (pi + (1 - pi) * survive)
    alive = rng.random(len(path)) >= q
    pay = np.full(len(path), -1, dtype=np.int64)
    k = c + 1
    start = r.copy()  # first week: only offsets >= r remain
    f = np.where(start > 0, np.take_along_axis(np.cumsum(probs, axis=1), np.maximum(start - 1, 0)[:, None], 1)[:, 0], 0.0)
    active = alive.copy()
    n = len(path)
    while active.any():
        u_hit, u_off = rng.random(n), rng.random(n)
        a = np.flatnonzero(active)
        week_start = issue[a] + 7 * (k[a] - 1)
        # past weeks: posterior and old terms; future weeks: base chances (shock hook) and new terms
        base = chance(a, k[a], due[a], pe, p, hz)
        if based or due_new is not due:
            fut = chance(a, k[a], due_new[a], pe_b if based else pe, p_b if based else p, hz_b)
            base = np.where(week_start >= 1, fut, base)
        h = _sigmoid_shift(base, None if shift_fn is None else shift_fn(path[a], b[a], week_start))
        if mult is not None:
            h = np.where(week_start >= 1, np.minimum(h * mult[a], 1.0), h)
        for in_w, factor, last in windows:
            h = np.where(in_w[a] & (week_start >= 1) & (week_start <= last), np.minimum(h * factor, 1.0), h)
        ff = f[a]
        hit = u_hit[a] < h * (1 - ff) / (1 - h * ff)
        off = _pick_offset(u_off[a], probs[a], start[a])
        day = week_start + off
        paid = a[hit]
        pay[paid] = day[hit]
        active[paid] = False
        k[a] += 1
        f[a] = 0.0
        start[a] = 0
        stop = active & (issue + 7 * (k - 1) > horizon)
        active[stop] = False
    pay[pay > horizon] = -1
    pay[(pay >= 0) & (pay < 1)] = 1  # paid inside the lag window: lands on day 1
    return pay


def _hazard_matrix(hazards: list[np.ndarray | None]) -> np.ndarray:
    """(n_buyers, N_BINS), NaN rows for buyers without an empirical hazard."""
    out = np.full((len(hazards), N_BINS), np.nan)
    for i, h in enumerate(hazards):
        if h is not None:
            out[i] = h
    return out


def _flat_index(inp: EngineInputs, buyer: np.ndarray, draw: np.ndarray) -> np.ndarray:
    offsets = np.cumsum([0] + [len(x) for x in inp.timing.p[:-1]])
    return offsets[buyer] + draw


def _open_survival(inp: EngineInputs, inv: Invoices) -> np.ndarray:
    """(n_open, max_draws) survival to the cut per invoice and posterior draw.

    Survival only involves weeks up to the cut, whose shocks are already known,
    so it does not depend on the path beyond the draw (shift evaluated at path 0).
    """
    n_draws = max(len(x) for x in inp.timing.p)
    out = np.ones((len(inv.buyer), n_draws))
    for i in range(len(inv.buyer)):
        b = inv.buyer[i]
        pe, p = inp.timing.p_early[b], inp.timing.p[b]
        elapsed = max(0, inp.survival_through_day - int(inv.issue_day[i]) + 1)
        c, r = divmod(elapsed, 7)
        probs = _offset_probs(inp, np.array([b]), np.array([inv.issue_day[i]]))[0]
        s = np.ones(len(p))
        for k in range(1, c + 2):
            if k == c + 1 and r == 0:
                break
            ws = int(inv.issue_day[i]) + 7 * (k - 1)
            shift = None if inp.shift is None else inp.shift(np.zeros(1, int), np.array([b]), np.array([ws]))
            h = _sigmoid_shift(pe if k < inv.due_week[i] else p, shift)
            s *= (1 - h) if k <= c else (1 - h * probs[:r].sum())
        out[i, : len(s)] = s
    return out


@dataclass
class EngineResult:
    run: dict
    balance: np.ndarray  # (n_paths, horizon + 1) int64, column 0 = opening
    component_totals: dict[str, np.ndarray]  # per-path totals by component
    flows: dict[str, np.ndarray] | None  # (n_paths, horizon + 1) per component, when keep_flows
    balance_quantiles: pl.DataFrame  # forecast.balance_quantiles
    shortfall: pl.DataFrame  # forecast.shortfall
    open_pay: np.ndarray | None = None  # (n_paths, n_open) payment day per open invoice, -1 none; keep_flows only

    def contract_tables(self) -> dict[str, pl.DataFrame]:
        return {"forecast.runs": run_table(self.run), "forecast.balance_quantiles": self.balance_quantiles,
                "forecast.shortfall": self.shortfall}


def run_engine(inp: EngineInputs, run_id: str, n_paths: int = 10_000, horizon: int = 90,
               overrides: list[OneOffEvent] | None = None, keep_flows: bool = False,
               seed_id: str | None = None) -> EngineResult:
    """seed_id: the id the random streams derive from (default run_id). A scenario run passes its
    baseline's run_id, so both use the same random numbers."""
    overrides = overrides or []
    seed_id = seed_id or run_id
    rng = {s: rng_for(seed_id, f"engine:{s}") for s in STREAMS}
    sc = inp.scenario
    rng_sc = rng_for(seed_id, "engine:scenario")
    H = horizon
    flows: dict[str, np.ndarray] = {}

    def add(name: str, path: np.ndarray | None, day: np.ndarray, amount: np.ndarray):
        arr = flows.setdefault(name, np.zeros((n_paths, H + 1), dtype=np.int64))
        keep = (day >= 1) & (day <= H)
        if path is None:  # same flow on every path
            np.add.at(arr, (slice(None), day[keep]), amount[keep])
        else:
            np.add.at(arr, (path[keep], day[keep]), amount[keep])

    # 1. One posterior draw per (path, buyer).
    n_b = len(inp.buyer_ids)
    draw_idx = (np.stack([rng["timing_draws"].integers(0, len(inp.timing.p[b]), n_paths) for b in range(n_b)], axis=1)
                if n_b else np.zeros((n_paths, 0), dtype=np.int64))  # a tenant with no buyers (e.g. a personal account)

    shift_fn = inp.shift_sampler(n_paths, H, rng["shocks"]) if inp.shift_sampler is not None else inp.shift

    # 3. Open invoices.
    oi = inp.open_invoices
    open_pay = None
    if len(oi.buyer):
        surv = _open_survival(inp, oi)
        path = np.repeat(np.arange(n_paths), len(oi.buyer))
        idx = np.tile(np.arange(len(oi.buyer)), n_paths)
        s = surv[idx, draw_idx[path, oi.buyer[idx]]]
        pay = _simulate_payments(inp, rng["open_invoices"], path, idx, oi, draw_idx, s, H, shift_fn)
        amount = oi.cash[idx]
        if sc is not None and sc.invoice_cash:
            amount = amount.copy()
            for i, day, amt, mode in sc.invoice_cash:
                rows = idx == i
                if mode == "if_unpaid":
                    rows &= (pay < 0) | (pay > day)
                pay[rows] = day if day <= H else -1
                amount[rows] = amt
        add("open_invoice_receipts", path, pay, amount)
        open_pay = pay.reshape(n_paths, len(oi.buyer))

    # 2, 4, 5. Sales: future invoices and linked purchases.
    month_of_day = np.array([month_key(inp.as_of + timedelta(days=d)) for d in range(H + 1)])
    path_month_net: dict[int, np.ndarray] = {}
    if inp.sales is not None:
        extra = {} if sc is None or sc.order_mult == 1.0 else {"order_mult": sc.order_mult, "extra_rng": rng_sc,
                                                                "purchase_sticky_days": sc.purchase_sticky_days}
        fut, purch = inp.sales(n_paths, H, rng["sales"], **extra)
        if len(fut.buyer):
            ones = np.ones(len(fut.buyer))
            pay = _simulate_payments(inp, rng["sales_timing"], fut.path, np.arange(len(fut.buyer)), fut,
                                     draw_idx, ones, H, shift_fn)
            add("future_invoice_receipts", fut.path, pay, fut.cash)
            for m in np.unique(month_of_day):
                sel = month_of_day[np.clip(fut.issue_day, 0, H)] == m
                sel &= (fut.issue_day >= 1) & (fut.issue_day <= H)
                path_month_net[m] = path_month_net.get(m, np.zeros(n_paths)) + np.bincount(
                    fut.path[sel], weights=fut.taxable[sel], minlength=n_paths)
        if purch.height:
            pp, pd_, pa = purch["path"].to_numpy(), purch["day"].to_numpy(), purch["amount_paise"].to_numpy()
            if sc is not None and sc.purchase_shift_days:
                pd_ = np.maximum(pd_ + sc.purchase_shift_days, 1)
            pieces = [(pd_, pa)]
            if sc is not None and sc.purchase_partial_shifts:
                rest, pieces = pa.copy(), []
                for share, days in sc.purchase_partial_shifts:
                    moved = np.round(pa * share).astype(np.int64)
                    pieces.append((np.maximum(pd_ + days, 1), moved))
                    rest = rest - moved
                pieces.append((pd_, rest))
            for p_day, p_amt in pieces:
                add("purchases", pp, p_day, -p_amt)
                for m in np.unique(month_of_day):
                    sel = month_of_day[np.clip(p_day, 0, H)] == m
                    path_month_net[m] = path_month_net.get(m, np.zeros(n_paths)) - np.bincount(
                        pp[sel], weights=p_amt[sel] / (1 + inp.gst_rate), minlength=n_paths)

    # 6. GST: per path, months in order, credit carried forward.
    if inp.gst_due_days:
        carry = np.full(n_paths, inp.gst_carry_in)
        for day in sorted(inp.gst_due_days):
            due_date = inp.as_of + timedelta(days=day)
            base = month_key(due_date) - 1
            net = inp.gst_history_net.get(base, 0.0) + path_month_net.get(base, np.zeros(n_paths))
            due = inp.gst_rate * net + carry
            carry = np.minimum(due, 0.0)
            add("gst", np.arange(n_paths), np.full(n_paths, day), -np.round(np.maximum(due, 0.0)).astype(np.int64))

    # 7. Fixed obligations, unscheduled flows, overrides.
    if inp.fixed:
        days, amounts = np.array([d for d, _ in inp.fixed]), np.array([a for _, a in inp.fixed], dtype=np.int64)
        add("fixed_obligations", None, days, -amounts)
    if inp.unscheduled is not None:
        u = inp.unscheduled(n_paths, H, rng["unscheduled"]).astype(np.int64)
        u[:, 0] = 0
        flows["unscheduled"] = u
    for ev in overrides:
        day = (ev.on - inp.as_of).days
        add("overrides", None, np.array([day]), np.array([ev.amount_paise], dtype=np.int64))
    for first, last, amount in (sc.cash if sc is not None else ()):
        days = rng_sc.integers(first, last + 1, n_paths) if last > first else np.full(n_paths, first)
        add("scenario_cash", np.arange(n_paths), days, np.full(n_paths, amount, dtype=np.int64))

    # 8. Balance. A freeze takes a share of the balance before it starts out of use and gives it
    # back after `days` days, as two flows, so balances still add up from the flows.
    def _balance():
        total = sum(flows.values()) if flows else np.zeros((n_paths, H + 1), dtype=np.int64)
        bal = inp.opening_balance_paise + np.cumsum(total, axis=1)
        bal[:, 0] = inp.opening_balance_paise
        return bal

    balance = _balance()
    for start, days, frac in (sc.freezes if sc is not None else ()):
        if not 1 <= start <= H:
            continue
        frozen = np.round(frac * np.maximum(balance[:, start - 1], 0)).astype(np.int64)
        add("freeze", np.arange(n_paths), np.full(n_paths, start), -frozen)
        add("freeze", np.arange(n_paths), np.full(n_paths, start + days), frozen)
        balance = _balance()

    run = {
        "run_id": run_id,
        "tenant_id": inp.tenant_id,
        "as_of": inp.as_of,
        "seed": seed_from_run_id(seed_id),
        "seed_streams": {s: seed_from_run_id(seed_id, f"engine:{s}") for s in STREAMS},
        "model_versions": inp.model_versions,
        "input_snapshot_ids": inp.input_snapshot_ids,
        "scenario_id": None,
        "overrides_json": json.dumps([{**asdict(o), "on": o.on.isoformat()} for o in overrides]),
        "n_paths": n_paths,
        "horizon_days": H,
    }
    bq, sf, runway = summarise(balance, inp.threshold_paise, inp.as_of, run_id, inp.tenant_id)
    run["runway_days"] = runway
    run["threshold_paise"] = inp.threshold_paise
    return EngineResult(run=run, balance=balance,
                        component_totals={k: v.sum(axis=1) for k, v in flows.items()},
                        flows=flows if keep_flows else None,
                        balance_quantiles=bq, shortfall=sf, open_pay=open_pay if keep_flows else None)


def run_table(run: dict) -> pl.DataFrame:
    """The forecast.runs row for a run record."""
    from matsya_contracts.validate import schema

    return pl.DataFrame([{
        "tenant_id": run["tenant_id"], "run_id": run["run_id"], "as_of": run["as_of"],
        "input_snapshot_ids": run["input_snapshot_ids"],
        "model_versions_json": json.dumps(run["model_versions"], sort_keys=True), "seed": run["seed"],
        "seed_streams_json": json.dumps(run["seed_streams"], sort_keys=True), "scenario_id": run["scenario_id"],
        "threshold_paise": run["threshold_paise"], "overrides_json": run["overrides_json"],
        "overrides_hash": hashlib.sha256(run["overrides_json"].encode()).hexdigest(),
        "n_paths": run["n_paths"], "horizon_days": run["horizon_days"], "runway_days": run["runway_days"],
    }], schema=schema("forecast.runs"))


def summarise(balance: np.ndarray, threshold: int, as_of: date, run_id: str, tenant_id: str):
    """Fan-chart quantiles, p_short, expected shortfall and runway (docs/track-b-decisions.md).

    Frames are forecast.balance_quantiles and forecast.shortfall in contract shape.
    """
    b = balance[:, 1:]
    H = b.shape[1]
    dates = [as_of + timedelta(days=d) for d in range(1, H + 1)]
    qs = np.quantile(b, [0.05, 0.25, 0.5, 0.75, 0.95], axis=0, method="lower").astype(np.int64)
    bq = pl.DataFrame({"tenant_id": tenant_id, "run_id": run_id, "date": dates, "q05": qs[0], "q25": qs[1], "q50": qs[2],
                       "q75": qs[3], "q95": qs[4]})
    running_min = np.minimum.accumulate(b, axis=1)
    short = running_min < threshold
    p_short = short.mean(axis=0)
    depth = np.where(short, threshold - running_min, 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cond = np.where(short.any(axis=0), depth.sum(axis=0) / np.maximum(short.sum(axis=0), 1), 0.0)
    cond = np.round(cond).astype(np.int64)
    sf = pl.DataFrame({
        "tenant_id": tenant_id, "run_id": run_id, "date": dates, "day": np.arange(1, H + 1, dtype=np.int32),
        "p_short": p_short,
        "expected_shortfall_cond_paise": cond,  # average depth over paths short by date
        "expected_shortfall_uncond_paise": np.round(p_short * cond).astype(np.int64),
    })
    over = np.flatnonzero(p_short >= 0.5)
    runway = int(over[0] + 1) if over.size else None
    return bq, sf, runway
