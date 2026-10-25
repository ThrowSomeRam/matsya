"""Owner view: the one payload the owner screen reads.

build_owner_view(tenant_run) -> OwnerView. Every number comes from the simulation engine, the decision
engine, the scenario engine and attribution (present.tenant_run); nothing is estimated here. Plain-language
strings are filled from fixed templates, and every number a string shows is also a field of the payload
(Money.text, Pct.text or an integer field next to it), so the screen never shows a figure the payload
does not hold. No LLM.

Money is in Indian format: Rs 4.2 lakh, Rs 1.3 crore, and en-IN digit grouping below a lakh (Rs 45,300).
Chances are whole percentages ("34%"); a chance under 1% that is not zero reads "under 1%".
"""

import re
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from matsya_intelligence.present.tenant_run import BREAKING_TARGET, TenantRun, p_short_by_day

DAYS = (30, 60, 90)
CALIBRATED_DAYS = 30
TOP_DRIVERS = 10
LIKELY_DEAD = 0.5  # chance of never being paid at or above which a bill is "likely not coming"
SCHEMA_VERSION = "owner-view-v1"
MSME_ODR_NOTE = ("Claims for late-payment interest go to the MSME ODR portal (Samadhaan). Filing is your "
                 "choice; this is what the law says you are owed today.")


# ---------------------------------------------------------------- formatting


def group_en_in(n: int) -> str:
    """12345678 -> '1,23,45,678' (en-IN digit grouping)."""
    s = str(abs(int(n)))
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ("-" if n < 0 else "") + ",".join(parts + [tail]) if parts else ("-" if n < 0 else "") + tail


def _one_decimal(x: float) -> str:
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def fmt_rs(paise: int) -> str:
    """Rs 4.2 lakh / Rs 1.3 crore / Rs 45,300; negative amounts read 'minus Rs ...'."""
    rupees = abs(int(paise)) / 100
    if rupees >= 1e7:
        body = f"Rs {_one_decimal(rupees / 1e7)} crore"
    elif rupees >= 1e5:
        body = f"Rs {_one_decimal(rupees / 1e5)} lakh"
    else:
        body = f"Rs {group_en_in(int(round(rupees)))}"
    return ("minus " if paise < 0 else "") + body


def fmt_pct(p: float) -> str:
    if p <= 0:
        return "0%"
    if p < 0.005:
        return "under 1%"
    return f"{min(round(p * 100), 100)}%"


def plain_name(name: str | None) -> str:
    """'NAV GANESH FOODS PVT LTD' -> 'Nav Ganesh Foods Pvt Ltd'."""
    if not name:
        return "a buyer"
    return " ".join(w if w.isdigit() else w.capitalize() for w in name.split())


# ---------------------------------------------------------------- payload


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(_M):
    paise: int
    text: str


class Pct(_M):
    value: float
    text: str


def money(paise: int | float) -> Money:
    p = int(round(paise))
    return Money(paise=p, text=fmt_rs(p))


def pct(p: float) -> Pct:
    return Pct(value=float(p), text=fmt_pct(float(p)))


class RiskAt(_M):
    day: int
    date: date
    chance: Pct


class Headline(_M):
    balance_today: Money
    balance_date: date
    risk: list[RiskAt]
    runway_day: int | None
    runway_date: date | None
    runway_text: str
    drivers_sentence: str


class FanDay(_M):
    day: int
    date: date
    median_paise: int
    lo50_paise: int
    hi50_paise: int
    lo90_paise: int
    hi90_paise: int
    calibrated: bool


class Fan(_M):
    days: list[FanDay]
    threshold: Money
    calibrated_through_day: int
    note: str


class RiskDay(_M):
    day: int
    date: date
    before: float
    after: float | None = None


class Driver(_M):
    rank: int
    kind: str  # invoice | buyer
    driver_id: str
    buyer: str
    label: str
    amount: Money | None
    contribution: Pct  # fall in the 90-day chance of running short if this driver were neutralised
    contribution_es: Money  # fall in the average shortfall depth over all futures
    days_overdue: int | None = None
    days_to_due: int | None = None
    buyer_usual_days_late: int | None = None
    chance_never_paid: Pct | None = None
    reason: str


class PlanAction(_M):
    rank: int
    kind: str
    target_id: str | None
    description: str
    cost: Money
    risk_after: Pct
    amount: Money | None = None  # overdraft draw, or the bill sold on TReDS
    days: int | None = None  # supplier stretch length
    on_date: date | None = None  # overdraft draw date


class Plan(_M):
    method: str
    actions: list[PlanAction]
    n_steps: int
    total_cost: Money
    risk_before: Pct
    risk_after: Pct
    target: Pct
    target_met: bool
    summary: str
    caveat: str | None


class WhatIf(_M):
    preset: str
    name: str
    description: str  # from a template over the preset's transforms; its figures are the fields below
    share: Pct | None = None
    amount: Money | None = None
    days: int | None = None
    days_from: int | None = None
    on_date: date | None = None
    risk_90: Pct
    change_90: float  # signed change in the 90-day chance against the baseline
    change_text: str
    runway_lost_days: int
    runway_text: str
    breaking_point: str
    breaking_magnitude: float | None
    breaking_share: Pct | None = None
    breaking_amount: Money | None = None
    breaking_days: int | None = None
    fan: list[FanDay]
    risk_by_day: list[float]


class Owed(_M):
    invoice_id: str
    buyer: str
    amount: Money
    days_late: int
    interest: Money


class OwedBlock(_M):
    items: list[Owed]
    total_interest: Money
    note: str


class Meta(_M):
    schema_version: str
    tenant_id: str
    run_id: str
    as_of: date
    data_through: date
    latest_data_received: datetime | None
    data_lag_days: int
    data_freshness: str
    model_versions: dict[str, str]
    plan_method: str
    plan_run_id: str
    n_paths: int


class OwnerView(_M):
    headline: Headline
    fan: Fan
    risk_by_day: list[RiskDay]
    drivers: list[Driver]
    plan: Plan
    what_ifs: list[WhatIf]
    owed: OwedBlock
    meta: Meta


# ---------------------------------------------------------------- pieces


def _date(as_of: date, day: int) -> date:
    return as_of + timedelta(days=day)


def _runway(p: np.ndarray) -> int | None:
    from matsya_intelligence.statutory import runway_p_short

    over = np.flatnonzero(p >= runway_p_short())
    return int(over[0] + 1) if over.size else None


def _runway_text(day: int | None, as_of: date) -> str:
    if day is None:
        return "beyond 90 days"
    return f"{day} days ({_date(as_of, day).strftime('%d %b %Y').lstrip('0')})"


def calibrated_fan(res, as_of: date, calibrator=None) -> list[FanDay]:
    """Per day 1..H: median, 50% and 90% bands, the engine's own quantiles (forecast.balance_quantiles).
    Days 1-30 are read at the calibration component's level instead: the nominal miscoverage (0.5, 0.1)
    scaled by its level / target at that horizon, interpolated between the calibrated horizons (1, 7, 14,
    30), with the engine's quantile rule. With no calibration history the scale is 1 and the band is the
    engine's."""
    q = res.balance_quantiles
    ratio = None
    if calibrator is not None:
        hs = [h for h in calibrator.horizons if h <= CALIBRATED_DAYS]
        ratio = (hs, [calibrator.level(h) / calibrator.alpha for h in hs])
    out = []
    for d in range(1, q.height + 1):
        cal = d <= CALIBRATED_DAYS
        r = float(np.interp(d, *ratio)) if cal and ratio else 1.0
        if r == 1.0:
            lo90, lo50, med, hi50, hi90 = (int(q[c][d - 1]) for c in ("q05", "q25", "q50", "q75", "q95"))
        else:
            a50, a10 = min(max(0.5 * r, 0.0), 1.0), min(max(0.1 * r, 0.0), 1.0)
            lv = [a10 / 2, a50 / 2, 0.5, 1 - a50 / 2, 1 - a10 / 2]
            lo90, lo50, med, hi50, hi90 = (int(v) for v in np.quantile(res.balance[:, d], lv, method="lower"))
        out.append(FanDay(day=d, date=_date(as_of, d), median_paise=med, lo50_paise=lo50, hi50_paise=hi50,
                          lo90_paise=lo90, hi90_paise=hi90, calibrated=cal))
    return out


def _usual_days_late(tr: TenantRun) -> dict[str, int]:
    """Per buyer: median days from due date to payment over its paid invoices."""
    paid = tr.outcomes.filter(pl.col("paid_date").is_not_null()).select("invoice_id", "paid_date").join(
        tr.invoices.select("invoice_id", "buyer_id", "due_date"), on="invoice_id")
    g = paid.group_by("buyer_id").agg(((pl.col("paid_date") - pl.col("due_date")).dt.total_days()).median().alias("d"))
    return {b: int(round(d)) for b, d in g.iter_rows()}


def _never(tr: TenantRun) -> dict[str, float]:
    return dict(tr.timing.predict(1).select("invoice_id", "p_never").iter_rows())


def _late_phrase(days: int) -> str:
    if days <= 0:
        return "usually pays on time"
    return f"usually pays {days} days after the due date"


def _drivers(tr: TenantRun, names: dict, usual: dict, never: dict) -> list[Driver]:
    inv = {r["invoice_id"]: r for r in tr.invoices.iter_rows(named=True)}
    oi, ids = tr.inputs.open_invoices, list(tr.inputs.open_invoice_ids)
    cash = {iid: int(oi.cash[i]) for i, iid in enumerate(ids)}
    open_by_buyer: dict[str, int] = {}
    for iid, c in cash.items():
        b = inv[iid]["buyer_id"]
        open_by_buyer[b] = open_by_buyer.get(b, 0) + c
    out = []
    for r in tr.attribution.table.head(TOP_DRIVERS).iter_rows(named=True):
        kind, did = r["driver_kind"], r["driver_id"]
        contrib, es = pct(max(r["delta_p_short_90"], 0.0)), money(max(r["delta_es_90_paise"], 0))
        if kind == "invoice":
            row = inv[did]
            b = row["buyer_id"]
            who = plain_name(names.get(b))
            amt = money(cash[did])
            late = (tr.as_of - row["due_date"]).days
            pn = float(never.get(did, 0.0))
            usual_late = usual.get(b)
            habit = "" if usual_late is None else f"; this buyer {_late_phrase(usual_late)}"
            if pn >= LIKELY_DEAD:
                when = f"{late} days overdue and " if late > 0 else ""
                reason = f"{when}likely not coming ({fmt_pct(pn)} chance it is never paid){habit}"
            elif late > 0:
                reason = f"{late} days overdue{habit}"
            else:
                reason = f"due in {-late} days{habit}"
            out.append(Driver(rank=r["rank"], kind=kind, driver_id=did, buyer=who, label=f"{amt.text} from {who}",
                              amount=amt, contribution=contrib, contribution_es=es,
                              days_overdue=max(late, 0), days_to_due=-late if late <= 0 else None,
                              buyer_usual_days_late=usual_late, chance_never_paid=pct(pn), reason=reason))
        else:
            who = plain_name(names.get(did))
            amt = money(open_by_buyer[did]) if did in open_by_buyer else None
            usual_late = usual.get(did)
            reason = (f"pays more slowly than similar buyers; it {_late_phrase(usual_late)}" if usual_late is not None
                      else "pays more slowly than similar buyers")
            out.append(Driver(rank=r["rank"], kind=kind, driver_id=did, buyer=who,
                              label=f"{who}, all bills" + ("" if amt is None else f" ({amt.text} open)"), amount=amt,
                              contribution=contrib, contribution_es=es, buyer_usual_days_late=usual_late,
                              reason=reason))
    return out


def _drivers_sentence(drivers: list[Driver]) -> str:
    top = [d for d in drivers if d.contribution.value > 0][:3]
    if not top:
        return "No single bill or buyer drives the risk."
    bits = []
    for d in top:
        if d.kind == "invoice":
            tail = ("which is likely not coming" if d.chance_never_paid and d.chance_never_paid.value >= LIKELY_DEAD
                    else (f"{d.days_overdue} days overdue" if d.days_overdue else f"due in {d.days_to_due} days"))
            bits.append(f"{d.amount.text} from {d.buyer}, {tail}")
        else:
            bits.append(f"{d.buyer} paying slowly")
    if len(bits) == 1:
        return f"Most of the risk is {bits[0]}."
    return f"Most of the risk is {bits[0]}" + "".join(f"; then {b}" for b in bits[1:-1]) + f"; and {bits[-1]}."


def _action(a, k: int, names: dict, inv: dict, as_of: date) -> dict:
    """Plain description of one plan action, with the figures it shows."""
    d = a.detail
    if a.kind == "overdraft":
        day = int(d.get("draw_day", 1))
        amt = money(k * a.unit_paise)
        when = "today" if day <= 1 else f"on {_date(as_of, day).strftime('%d %b %Y').lstrip('0')}"
        return {"description": f"Draw {amt.text} from your overdraft {when}", "amount": amt,
                "on_date": _date(as_of, day)}
    if a.kind == "treds_discount":
        row = inv.get(a.target_id, {})
        amt = money(row.get("gross_paise", 0))
        return {"description": f"Get early cash for the {amt.text} bill to {plain_name(names.get(row.get('buyer_id')))} "
                               "through TReDS", "amount": amt}
    if a.kind == "chase":
        return {"description": f"Call {plain_name(names.get(a.target_id))} about their open bills, this week and next"}
    if a.kind == "supplier_stretch":
        sid = a.target_id.split(":")[0]
        days = int(d["days"])
        return {"description": f"Ask supplier {sid} to wait {days} more days for its payments", "days": days}
    return {"description": a.kind.replace("_", " ").capitalize()}


def _plan(tr: TenantRun, names: dict, inv: dict) -> Plan:
    pr = tr.plan
    acts = []
    for (a, k), r in zip(pr.chosen, pr.plan.iter_rows(named=True)):
        acts.append(PlanAction(rank=r["rank"], kind=a.kind, target_id=r["target_id"], cost=money(r["cost_paise"]),
                               risk_after=pct(r["p_short_after"]), **_action(a, k, names, inv, tr.as_of)))
    total = money(int(pr.plan["cost_paise"].sum()) if pr.plan.height else 0)
    before = pct(float(p_short_by_day(tr.baseline)[pr.day - 1]))
    after = pct(pr.p_short_verified)
    target = pct(pr.eps)
    met = pr.p_short_verified <= pr.eps
    caveat = None
    if pr.solver.get("time_limited"):
        caveat = ("The planner ran out of time before proving this is the cheapest plan; a cheaper one may exist. "
                  "The risk figures are checked on fresh futures either way.")
    elif pr.solver.get("status") not in (0, None):
        caveat = "The planner did not finish normally; treat this plan as a starting point."
    if not pr.target_met:
        extra = "No mix of the actions available reaches your target; this plan gets as close as it can."
        caveat = extra if caveat is None else f"{caveat} {extra}"
    if acts:
        summary = (f"{len(acts)} steps costing {total.text} take the chance of running short by day {pr.day} "
                   f"from {before.text} to {after.text} (your target: {target.text}).")
    else:
        summary = f"No action needed: the chance of running short by day {pr.day} is {before.text}."
    return Plan(method=pr.method, actions=acts, n_steps=len(acts), total_cost=total, risk_before=before, risk_after=after,
                target=target, target_met=met, summary=summary, caveat=caveat)


def _breaking(ts: list, bp, base_p90: float) -> dict:
    """The breaking-point sentence and the figure it shows (share, amount or days)."""
    if bp is None:
        return {"breaking_point": "No breaking point for this one."}
    if base_p90 >= BREAKING_TARGET:
        return {"breaking_point": "You are already more likely than not to run short by day 90 without this."}
    if bp.magnitude is None:
        return {"breaking_point": "This alone does not make running short by day 90 more likely than not."}
    m, t = bp.magnitude, ts[0]
    if t.type == "rate_multiplier":
        sh = pct(1 - m)
        who = "orders fall" if t.target == "orders" else "your buyers pay"
        tail = "" if t.target == "orders" else " slower"
        return {"breaking_point": f"You break if {who} {sh.text}{tail}.", "breaking_share": sh}
    if t.type == "cash_event":
        amt = money(abs(int(m)))
        return {"breaking_point": f"You break if that one-off bill is {amt.text} or more.", "breaking_amount": amt}
    if t.type == "terms_change":
        who = "suppliers cut your credit" if t.party == "supplier" else "buyers stretch their terms"
        return {"breaking_point": f"You break if {who} to {int(m)} days.", "breaking_days": int(m)}
    if t.type == "date_shift":
        way = "earlier" if m < 0 else "later"
        return {"breaking_point": f"You break if it moves {abs(int(m))} days {way}.", "breaking_days": abs(int(m))}
    return {"breaking_point": "This one has a breaking point; see the figures."}


PRESET_NAMES = {
    "delayed_customer_payments": "Buyers pay slower",
    "unexpected_expense": "An unexpected bill",
    "reduced_sales": "Orders fall",
    "supplier_tightens_terms": "Suppliers want paying sooner",
    "revised_payment_schedule": "EMI moves earlier",
}


def _describe(ts: list, as_of: date) -> dict:
    """Plain description of a preset's first transform and the figures it shows."""
    if not ts:
        return {"description": "No change."}
    t = ts[0]
    if t.type == "rate_multiplier" and t.target == "orders":
        sh = pct(1 - t.factor)
        return {"description": f"Orders fall by {sh.text}. Supplies already ordered are still paid for.", "share": sh}
    if t.type == "rate_multiplier":
        sh = pct(t.factor)
        return {"description": f"Buyers pay more slowly: their chance of paying in any week drops to {sh.text} of normal.",
                "share": sh}
    if t.type == "cash_event":
        amt = money(abs(t.amount_paise))
        return {"description": f"A one-off bill of {amt.text} on {t.on.strftime('%d %b %Y').lstrip('0')}.",
                "amount": amt, "on_date": t.on}
    if t.type == "terms_change" and t.party == "supplier":
        return {"description": f"Suppliers cut your credit from {t.from_days} to {t.to_days} days, so you pay them sooner.",
                "days": int(t.to_days), "days_from": int(t.from_days)}
    if t.type == "date_shift":
        what = ", ".join(t.ids) if t.ids else "supplier payments"
        what = what.upper() if what == "emi" else what
        return {"description": f"Your {what} payment moves {abs(t.days)} days {'earlier' if t.days < 0 else 'later'}.",
                "days": abs(int(t.days))}
    return {"description": t.type.replace("_", " ").capitalize()}


def _plain_change(base: float, new: float) -> str:
    if fmt_pct(base) == fmt_pct(new):
        return f"The chance of running short within 90 days stays at {fmt_pct(new)}."
    return f"The chance of running short within 90 days goes from {fmt_pct(base)} to {fmt_pct(new)}."


def _what_ifs(tr: TenantRun) -> list[WhatIf]:
    out = []
    base_p = p_short_by_day(tr.baseline)
    for key, entry, sr, bp in tr.scenarios:
        p = sr.result.shortfall["p_short"].to_numpy()
        dp = float(p[89] - base_p[89])
        lost = int(sr.runway_lost_days)
        if lost <= 0:
            rw = "No runway lost" + (" within 90 days." if sr.runway_lost_is_lower_bound else ".")
        else:
            rw = f"{'At least ' if sr.runway_lost_is_lower_bound else ''}{lost} days of runway lost."
        out.append(WhatIf(preset=key, name=PRESET_NAMES.get(key, key.replace("_", " ").capitalize()),
                          **_describe(sr.transforms, tr.as_of),
                          risk_90=pct(float(p[89])), change_90=dp, change_text=_plain_change(float(base_p[89]), float(p[89])),
                          runway_lost_days=lost, runway_text=rw,
                          **_breaking(sr.transforms, bp, float(base_p[89])),
                          breaking_magnitude=None if bp is None else bp.magnitude,
                          fan=calibrated_fan(sr.result, tr.as_of, None),
                          risk_by_day=[float(x) for x in p]))
    return out


def _owed(tr: TenantRun, names: dict) -> OwedBlock:
    o = tr.plan.msmed_owed.filter(pl.col("interest_to_as_of_paise").is_not_null() & (pl.col("days_overdue") > 0))
    o = o.sort(["interest_to_as_of_paise", "invoice_id"], descending=[True, False])
    items = [Owed(invoice_id=r["invoice_id"], buyer=plain_name(names.get(r["buyer_id"])), amount=money(r["principal_paise"]),
                  days_late=int(r["days_overdue"]), interest=money(r["interest_to_as_of_paise"]))
             for r in o.iter_rows(named=True)]
    return OwedBlock(items=items, total_interest=money(sum(i.interest.paise for i in items)), note=MSME_ODR_NOTE)


def _freshness(tr: TenantRun) -> str:
    lag = (tr.as_of - tr.data_through).days
    through = tr.data_through.strftime("%d %b %Y").lstrip("0")
    return f"Bank data complete up to {through}" + ("." if lag <= 0 else f", {lag} days before today.")


def build_owner_view(tenant_run: TenantRun) -> OwnerView:
    tr = tenant_run
    names = dict(zip(tr.buyers["buyer_id"], tr.buyers["name"]))
    inv = {r["invoice_id"]: r for r in tr.invoices.iter_rows(named=True)}
    base = tr.baseline
    p = p_short_by_day(base)
    runway = _runway(p)
    usual, never = _usual_days_late(tr), _never(tr)
    drivers = _drivers(tr, names, usual, never)
    after = tr.plan.p_short_by_day["plan"] if tr.plan.p_short_by_day else None
    headline = Headline(
        balance_today=money(tr.inputs.opening_balance_paise), balance_date=tr.as_of,
        risk=[RiskAt(day=d, date=_date(tr.as_of, d), chance=pct(float(p[d - 1]))) for d in DAYS],
        runway_day=runway, runway_date=None if runway is None else _date(tr.as_of, runway),
        runway_text=_runway_text(runway, tr.as_of), drivers_sentence=_drivers_sentence(drivers))
    fan = Fan(days=calibrated_fan(base, tr.as_of, tr.calibrator), threshold=money(tr.inputs.threshold_paise),
              calibrated_through_day=CALIBRATED_DAYS,
              note=f"Days 1 to {CALIBRATED_DAYS} are calibrated: their range is checked against what actually happens and "
                   "adjusted as your history builds up. Later days are the model's own range.")
    risk = [RiskDay(day=d, date=_date(tr.as_of, d), before=float(p[d - 1]),
                    after=None if after is None else float(after[d - 1])) for d in range(1, len(p) + 1)]
    mv = {k: str(v) for k, v in sorted(base.run["model_versions"].items())}
    mv["decision_engine"] = str(tr.plan.record["model_version"])
    meta = Meta(schema_version=SCHEMA_VERSION, tenant_id=tr.tenant_id, run_id=tr.run_id, as_of=tr.as_of,
                data_through=tr.data_through, latest_data_received=tr.latest_known_at,
                data_lag_days=(tr.as_of - tr.data_through).days, data_freshness=_freshness(tr),
                model_versions=mv, plan_method=tr.plan.method, plan_run_id=tr.plan.run_id,
                n_paths=int(base.balance.shape[0]))
    return OwnerView(headline=headline, fan=fan, risk_by_day=risk, drivers=drivers, plan=_plan(tr, names, inv),
                     what_ifs=_what_ifs(tr), owed=_owed(tr, names), meta=meta)


# ---------------------------------------------------------------- checks used by the tests


NUMBER = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")


def strings_and_numbers(view: OwnerView) -> tuple[list[str], set[str]]:
    """(every plain-language string, every number token the payload holds as a figure: Money and Pct texts,
    and integer fields). A string may only show number tokens from the second set (dates aside)."""
    texts: list[str] = []
    allowed: set[str] = set()

    def walk(x, key=""):
        if isinstance(x, (Money, Pct)):
            allowed.update(NUMBER.findall(x.text))
            return
        if isinstance(x, BaseModel):
            for k in type(x).model_fields:
                walk(getattr(x, k), k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v, key)
        elif isinstance(x, bool):
            return
        elif isinstance(x, int):
            allowed.add(str(abs(x)))
            allowed.add(group_en_in(abs(x)))
        elif isinstance(x, (date, datetime)):
            allowed.update(NUMBER.findall(x.strftime("%d %b %Y").lstrip("0")))
            allowed.update(NUMBER.findall(x.strftime("%d %b %Y")))
        elif isinstance(x, str) and key in ("reason", "drivers_sentence", "summary", "caveat", "description", "label",
                                            "breaking_point", "runway_text", "change_text", "note", "data_freshness",
                                            "name"):
            texts.append(x)

    walk(view)
    return texts, allowed
