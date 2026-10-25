"""Simulate one supplier's books end to end, then cut them at as_of.

Randomness is split at as_of. Every draw that decides something on or before
as_of comes from past streams (past_seed); every draw that decides something
after it comes from future streams (past_seed, future_seed). So one past_seed
fixes the observed tables exactly, and varying future_seed resamples only the
future, which is what resample_futures() uses to get oracle probabilities.

Invoices still open at as_of get their payment timing redrawn from the future
streams conditional on "not paid by as_of" (exact for geometric timing,
rejection sampling for the misspecification knobs).

Orders are placed on the purchase date; the invoice is issued purchase_lag
days later. So purchases made before as_of belong to past orders even when
their invoice is issued after as_of.
"""

import calendar
import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from matsya_contracts import ids
from matsya_contracts.validate import schema

from matsya_intelligence.categories import CATEGORIES
from matsya_intelligence.io.types import OBLIGATION, TXN
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth import narration as nr
from matsya_intelligence.synth.config import WorldConfig

# Which generating process each category comes from (see docs/track-b-decisions.md
# for which component owns each flow's date and amount).
PROCESS_OF_CATEGORY = {
    "customer_receipt": "sales",
    "supplier_payment": "purchase",
    "salary": "fixed_obligation",
    "rent": "fixed_obligation",
    "emi": "fixed_obligation",
    "professional_fee": "fixed_obligation",
    "tds_payment": "fixed_obligation",
    "pf_esi": "fixed_obligation",
    "gst_payment": "fixed_obligation",
    "bank_charges": "unscheduled",
    "refund": "unscheduled",
    "misc_income": "unscheduled",
    "cash_withdrawal": "unscheduled",
    "owner_transfer": "unscheduled",
    "misc_expense": "unscheduled",
}
assert set(PROCESS_OF_CATEGORY) == set(CATEGORIES)

CONSTANTS_PATH = Path(__file__).resolve().parents[3] / "config" / "track_b_constants.yaml"
TRUTH_BALANCE = {"tenant_id": pl.String, "account_id": pl.String, "date": pl.Date, "closing_paise": pl.Int64,
                 "continuity_ok": pl.Boolean}
PREROLL_WEEKS = 3  # orders start this early so the first weeks have their invoices
REJECTION_CAP = 100_000


@cache
def load_constants() -> dict:
    return yaml.safe_load(CONSTANTS_PATH.read_text())


@dataclass
class Truth:
    config: WorldConfig
    past_seed: int
    future_seed: int
    opening_balance_paise: int  # back-solved so the true balance at as_of is as_of_balance_paise
    sector_shocks: pl.DataFrame  # sector, week, week_start, z
    weekly_orders: pl.DataFrame  # week, week_start, demand_x, mean, count
    buyers: pl.DataFrame  # planted p, pi, loading, mix_share, tds_rate per buyer
    invoices: pl.DataFrame  # invoices issued in [start, end] with never_paid, pay_week, paid dates
    payments: pl.DataFrame  # one row per receipt: invoice_id, part, txn_uid, value_date, amounts
    purchases: pl.DataFrame  # purchases dated in [start, end]; invoice may be issued after end
    # Every txn incl. future: contract columns + category, rail, process, plus for renderers
    # bank_txn_id (the synthetic bank txnId behind txn_uid, key path txn:<id>|<date>|<amount>)
    # and day_seq (0-based posting order within value_date, for running balances).
    transactions: pl.DataFrame
    balances: pl.DataFrame  # daily closing from all txns
    obligations: pl.DataFrame  # every scheduled outflow, Obligation schema
    buyer_records: list[dict] = field(default_factory=list)  # full buyer draws (PAN, GSTIN, name), for re-cuts


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _last_day_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _months(start: date, end: date):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _irn(invoice_id: str) -> str:
    return hashlib.sha256(invoice_id.encode()).hexdigest()


def synth_tenant_id(name: str) -> str:
    """A deterministic UUIDv4-shaped tenant_id for a synthetic tenant name."""
    h = hashlib.sha256(name.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


def fiscal_year(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def generate_world(config: WorldConfig, past_seed: int, future_seed: int) -> tuple[dict[str, pl.DataFrame], Truth]:
    c = config
    consts = load_constants()
    tds_rates = consts["tds"]["matcher_candidate_rates"]
    cal = consts["statutory_calendar"]["monthly"]
    t0 = c.as_of_date
    end = t0 + timedelta(days=c.future_days)
    n_weeks = (end - c.start).days // 7 + 2
    n_past_weeks = (t0 - c.start).days // 7 + 1  # weeks starting on or before as_of
    past_id, future_id = f"synth-past-{past_seed}", f"synth-future-{past_seed}-{future_seed}"

    def streams(name: str) -> tuple[np.random.Generator, np.random.Generator]:
        return rng_for(past_id, name), rng_for(future_id, name)

    rn_past, rn_future = streams("narration")
    # One account, one bank format: the template family is drawn once per world.
    family = c.narration_variants[int(rng_for(past_id, "style").integers(len(c.narration_variants)))]
    st = nr.Style(variants=(family,), banks=c.narration_banks)

    def rn(d: date) -> np.random.Generator:
        return rn_past if d <= t0 else rn_future

    def week_of(d: date) -> int:
        return min(max((d - c.start).days // 7, 0), n_weeks - 1)

    # Sector shocks: stationary AR(1), one value per world week.
    rp, rf = streams("sectors")
    n_s = len(c.sectors)
    u = np.concatenate([rp.normal(size=(n_s, n_past_weeks)), rf.normal(size=(n_s, n_weeks - n_past_weeks))], axis=1)
    eps = c.shock_sigma * u
    z = np.zeros_like(eps)
    z[:, 0] = eps[:, 0] / math.sqrt(1 - c.shock_rho**2)
    for t in range(1, n_weeks):
        z[:, t] = c.shock_rho * z[:, t - 1] + eps[:, t]
    shocks = pl.DataFrame({
        "sector": [s for s in c.sectors for _ in range(n_weeks)],
        "week": [w for _ in c.sectors for w in range(n_weeks)],
        "week_start": [c.start + timedelta(days=7 * w) for _ in c.sectors for w in range(n_weeks)],
        "z": z.ravel(),
    })

    # Demand shock: AR(1) on the log order rate. Innovations correlate with the
    # standardised mean of the sector lateness innovations.
    rp, rf = streams("demand")
    common = u.mean(axis=0) * math.sqrt(n_s)
    own = np.concatenate([rp.normal(size=n_past_weeks), rf.normal(size=n_weeks - n_past_weeks)])
    e = c.demand_shock_corr * common + math.sqrt(1 - c.demand_shock_corr**2) * own
    var_x = c.demand_sigma**2 / (1 - c.demand_rho**2)
    x = np.zeros(n_weeks)
    x[0] = math.sqrt(var_x) * e[0]
    for t in range(1, n_weeks):
        x[t] = c.demand_rho * x[t - 1] + c.demand_sigma * e[t]
    order_mean = c.order_mean_per_week * np.exp(x - var_x / 2)

    # Buyers drawn from type-level hyperpriors; fixed shares of the order flow.
    r = rng_for(past_id, "buyers")
    types = list(c.type_priors)
    shares = np.array([c.type_priors[t].share for t in types])
    buyers = []
    for i in range(c.n_buyers):
        btype = str(r.choice(types, p=shares / shares.sum()))
        sector_idx = int(r.integers(n_s))
        tp = c.type_priors[btype]
        pan = nr.make_pan(r)
        buyers.append({
            "buyer_id": ids.buyer_id(pan),
            "index": i,
            "type": btype,
            "sector": c.sectors[sector_idx],
            "sector_idx": sector_idx,
            "p": float(r.beta(*tp.p_ab)),
            "pi": float(r.beta(*tp.pi_ab)),
            "loading": float(r.normal(c.loading_mean, c.loading_sd)),
            "tds_rate": float(r.choice(tds_rates)),
            "pan": pan,
            "gstin": nr.make_gstin(r, pan),
            "name": nr.company_name(r, c.sectors[sector_idx], btype),
        })
    mix = r.dirichlet(np.full(c.n_buyers, c.buyer_mix_concentration))
    r_early = rng_for(past_id, "buyers_early")  # own stream: other buyer draws stay unchanged
    for b in buyers:
        b["p_early"] = float(r_early.beta(*c.type_priors[b["type"]].p_early_ab))
    for b, share in zip(buyers, mix):
        b["mix_share"] = float(share)

    # Orders: NegBin weekly count (gamma-Poisson), placed on the purchase date.
    # The week containing as_of is split: the past stream draws a full week and
    # keeps days <= as_of, the future stream draws a full week and keeps the
    # rest (right mean, slightly less dispersion than one NB draw for that week).
    rp, rf = streams("orders")
    orders = []
    weekly = []

    def draw_week(r: np.random.Generator, w: int, week_start: date, keep) -> int:
        lam = r.gamma(c.order_dispersion, order_mean[max(w, 0)] / c.order_dispersion)
        kept = 0
        for _ in range(int(r.poisson(lam))):
            b = buyers[int(r.choice(c.n_buyers, p=mix))]
            order_date = week_start + timedelta(days=int(r.integers(7)))
            gross = int(round(c.amount_median_paise * math.exp(c.amount_sigma * r.normal())))
            terms = int(r.choice(c.terms_choices))
            lag = c.purchase_lag_days + int(r.integers(-c.purchase_lag_jitter_days, c.purchase_lag_jitter_days + 1))
            if keep(order_date):
                orders.append({"buyer": b, "order_date": order_date, "gross": gross, "terms": terms, "lag": lag})
                kept += 1
        return kept

    w = -PREROLL_WEEKS
    while (week_start := c.start + timedelta(days=7 * w)) <= end:
        count = 0
        if week_start <= t0:
            count += draw_week(rp, w, week_start, lambda d: d <= t0)
        if week_start + timedelta(days=6) > t0:
            count += draw_week(rf, w, week_start, lambda d: d > t0)
        if w >= 0:
            weekly.append({"week": w, "week_start": week_start, "demand_x": float(x[w]),
                           "mean": float(order_mean[w]), "count": count})
        w += 1

    txns: list[dict] = []

    def add_txn(d: date, amount: int, category: str, rail_narr: tuple[str, str]) -> int | None:
        if d < c.start or d > end or amount == 0:
            return None
        txns.append({"value_date": d, "amount_paise": amount, "category": category,
                     "rail": rail_narr[0], "narration_raw": rail_narr[1]})
        return len(txns) - 1

    # Purchases (paid on the order date) and the invoices they lead to.
    suppliers = [nr.company_name(rn_past, str(rn_past.choice(c.sectors)), "unlisted") for _ in range(8)]
    purchases, invoices = [], []
    for n, o in enumerate(orders):
        d = o["order_date"]
        amount = int(round(c.purchase_ratio * o["gross"]))
        g = rn(d)
        name = suppliers[int(g.integers(len(suppliers)))]
        idx = add_txn(d, -amount, "supplier_payment", nr.debit_to_party(g, st, name, d, f"BILL {g.integers(100, 9999)}", amount))
        inv_id = f"INV-{n:05d}"
        purchases.append({"invoice_id": inv_id, "txn_idx": idx, "value_date": d, "amount_paise": amount,
                          "lag_days": o["lag"]})
        issue = d + timedelta(days=o["lag"])
        gst = int(round(o["gross"] * c.gst_rate / (1 + c.gst_rate)))
        invoices.append({
            "invoice_id": inv_id,
            "buyer": o["buyer"],
            "issue_date": issue,
            "due_date": issue + timedelta(days=o["terms"]),
            "terms_days": o["terms"],
            "gross_paise": o["gross"],
            "gst_paise": gst,
            "tds_paise": int(round(o["buyer"]["tds_rate"] * (o["gross"] - gst))),
        })

    # Terms the platform could not read (own stream, drawn only when the knob is on).
    if c.terms_unknown_share > 0:
        r_terms = rng_for(past_id, "terms_source")
        for inv in invoices:
            inv["terms_unknown"] = bool(r_terms.random() < c.terms_unknown_share)

    # Payment timing: weekly hazard p shifted by the sector shock, cure fraction pi.
    rp, rf = streams("payments")
    tp_past, tp_future = streams("treds")
    bc = c.behaviour_change
    exact = c.lateness == "geometric" and not c.month_end_clustering
    if c.lateness not in ("geometric", "lognormal"):
        raise ValueError(f"unknown lateness {c.lateness!r}")
    payments = []
    for inv in invoices:
        b, issue = inv["buyer"], inv["issue_date"]
        inv.update(never_paid=False, pay_week=None, first_paid_date=None, paid_date=None, part_paid=False)
        if issue < c.start or issue > end:
            continue
        changed = bc is not None and bc.buyer_index == b["index"]
        pi = min(b["pi"] + bc.pi_add, 0.99) if changed and issue >= bc.at else b["pi"]

        due_week = inv["terms_days"] // 7 + 1

        def hazard(k: int, phase: str | None = None) -> float:
            d = issue + timedelta(days=7 * (k - 1))
            early = phase == "early" or (phase is None and c.pre_due_phase and k < due_week)
            p = b["p_early"] if early else b["p"]
            if changed and d >= bc.at:
                p = min(max(p * bc.p_multiplier, 1e-4), 1 - 1e-4)
            if c.payment_shock is not None and d >= c.payment_shock[0]:
                p = min(max(p * c.payment_shock[1], 1e-4), 1 - 1e-4)
            return _sigmoid(_logit(p) + b["loading"] * z[b["sector_idx"], week_of(d)])

        def draw(r: np.random.Generator, stop: date) -> tuple[bool, int | None, date | None]:
            """Unconditional draw of (never, pay_week, first date); weeks starting after stop are not tried."""
            if r.random() < pi:
                return True, None, None
            if c.lateness == "lognormal":
                days = (inv["terms_days"] + 7 / hazard(1, "late")) * math.exp(c.lognormal_sigma * r.normal())
                k = max(1, math.ceil(days / 7))
            else:
                k = 1
                while issue + timedelta(days=7 * (k - 1)) <= stop:
                    if r.random() < hazard(k):
                        break
                    k += 1
                else:
                    return False, None, None
            first = issue + timedelta(days=7 * (k - 1) + int(r.integers(7)))
            if c.month_end_clustering and r.random() < c.month_end_prob:
                first = _last_day_of_month(first)
            return False, k, first

        never, pay_week, first = False, None, None
        paid_in_past = False
        if issue <= t0:
            never, pay_week, first = draw(rp, t0)
            paid_in_past = first is not None and first <= t0
        if not paid_in_past:
            # Open at as_of (or issued after it): draw from the future streams,
            # conditional on not being paid by as_of.
            if exact:
                never, pay_week, first = _conditional_geometric(rf, issue, t0, end, pi, hazard)
            else:
                for _ in range(REJECTION_CAP):
                    never, pay_week, first = draw(rf, end)
                    if first is None or first > t0:
                        break
                else:
                    never, pay_week, first = True, None, None

        inv.update(never_paid=never, pay_week=pay_week)
        discount = None
        if c.treds_discount_prob > 0 and b["type"] == "cpse":
            # Decided at issue, so the past stream fixes it for invoices issued by as_of
            # even when the discount date falls after as_of (like a purchase's invoice).
            r_t = tp_past if issue <= t0 else tp_future
            if r_t.random() < c.treds_discount_prob:
                discount = issue + timedelta(days=int(r_t.integers(2, 8)))
        if discount is not None:
            net = inv["gross_paise"] - inv["tds_paise"]
            cash = int(round(net * (1 - c.treds_fee)))
            inv.update(first_paid_date=discount, paid_date=discount, treds_discount_date=discount)
            idx = add_txn(discount, cash, "customer_receipt",
                          nr.credit_via_treds(rn(discount), st, discount, inv["invoice_id"].replace("-", " ")))
            payments.append({"invoice_id": inv["invoice_id"], "part": 1, "txn_idx": idx, "value_date": discount,
                             "amount_paise": cash, "tds_paise": inv["tds_paise"], "payer_kind": "treds_financier"})
            continue
        if first is None:
            continue
        g = rp if first <= t0 else rf
        net = inv["gross_paise"] - inv["tds_paise"]
        parts = [(first, net, inv["tds_paise"])]
        if g.random() < c.part_payment_prob:
            f = g.uniform(0.3, 0.7)
            second = first + timedelta(days=int(g.integers(7, 29)))
            if first <= t0 < second:
                # Second part still unpaid at as_of: its date is future randomness.
                second = first + timedelta(days=int(rf.integers(max(7, (t0 - first).days + 1), 29)))
            n1, t1 = int(round(net * f)), int(round(inv["tds_paise"] * f))
            parts = [(first, n1, t1), (second, net - n1, inv["tds_paise"] - t1)]
            inv["part_paid"] = True
        inv["first_paid_date"], inv["paid_date"] = parts[0][0], parts[-1][0]
        for part_no, (d, amount, tds) in enumerate(parts, start=1):
            g = rn(d)
            ref = inv["invoice_id"].replace("-", " ") if g.random() < 0.7 else "PAYMENT"
            if b["type"] == "cpse":
                rail_narr = nr.credit_via_treds(g, st, d, ref)
            else:
                rail_narr = nr.credit_from_company(g, st, b["name"], d, ref, amount)
            idx = add_txn(d, amount, "customer_receipt", rail_narr)
            payments.append({"invoice_id": inv["invoice_id"], "part": part_no, "txn_idx": idx,
                             "value_date": d, "amount_paise": amount, "tds_paise": tds, "payer_kind": "buyer"})

    # Scheduled outflows. Each is both a txn and a truth obligation.
    obligations = []
    supplier_gstin = nr.make_gstin(rn_past, nr.make_pan(rn_past))
    landlord, owner, lender = nr.person_name(rn_past), nr.person_name(rn_past), str(rn_past.choice(nr.LENDERS))
    ca_firm = f"{nr.person_name(rn_past)} AND CO"
    tds_deducted: dict[tuple[int, int], int] = {}

    # GST for a month = gst_rate x (taxable sales issued - taxable purchases made),
    # with any excess input credit carried forward.
    taxable_net: dict[tuple[int, int], float] = {}
    for inv in invoices:
        key = (inv["issue_date"].year, inv["issue_date"].month)
        taxable_net[key] = taxable_net.get(key, 0.0) + inv["gross_paise"] - inv["gst_paise"]
    for p in purchases:
        key = (p["value_date"].year, p["value_date"].month)
        taxable_net[key] = taxable_net.get(key, 0.0) - p["amount_paise"] / (1 + c.gst_rate)
    gst_carry = 0.0

    def scheduled(kind: str, d: date, amount: int, category: str, rail_narr: tuple[str, str]):
        if add_txn(d, -amount, category, rail_narr) is not None:
            obligations.append({"obligation_id": f"OB-{kind}-{d:%Y%m}", "kind": kind,
                                "due_date": d, "amount_paise": amount, "confidence": 1.0,
                                "amount_owner": "sales_forecaster" if kind == "gst" else "obligations_detector"})

    for y, m in _months(c.start, end):
        py, pm = _prev_month(y, m)
        d = date(y, m, c.salary_day)
        scheduled("salary", d, c.salary_paise, "salary", nr.salary(rn(d), st, d))
        d = date(y, m, c.professional_fee_day)
        tds = int(round(float(consts["tds"]["rates"]["professional_fees"]["rate"]) * c.professional_fee_paise))  # 194J
        scheduled("professional_fee", d, c.professional_fee_paise - tds, "professional_fee",
                  nr.debit_to_party(rn(d), st, ca_firm, d, "PROF FEES", c.professional_fee_paise))
        if c.start <= d <= end:
            tds_deducted[(y, m)] = tds
        d = date(y, m, c.rent_day)
        # Real rent transfers often carry no keyword; the categoriser must use recurrence.
        note = str(rn(d).choice(["RENT", "RENT", "RENT", "MONTHLY", "TRANSFER"]))
        scheduled("rent", d, c.rent_paise, "rent", nr.debit_to_party(rn(d), st, landlord, d, note, c.rent_paise))
        d = date(y, m, c.emi_day)
        scheduled("emi", d, c.emi_paise, "emi", nr.ach_emi(rn(d), st, lender))
        # TDS for the previous month: 7th, except March deductions are due 30 April.
        d = date(y, 4, 30) if pm == 3 else date(y, m, cal["tds_deposit"]["day"])
        scheduled("tds", d, tds_deducted.get((py, pm), 0), "tds_payment", nr.tds_payment(rn(d), st, d))
        d = date(y, m, cal["pf_deposit"]["day"])
        scheduled("pf", d, int(round(c.pf_rate * c.salary_paise)), "pf_esi", nr.pf_payment(rn(d), st))
        d = date(y, m, cal["esi_deposit"]["day"])
        scheduled("esi", d, int(round(c.esi_rate * c.salary_paise)), "pf_esi", nr.esi_payment(rn(d), st))
        d = date(y, m, cal["gstr3b_monthly"]["day"])
        if d.weekday() == 6:  # Sunday moves to next working day
            d += timedelta(days=1)
        due = c.gst_rate * taxable_net.get((py, pm), 0.0) + gst_carry
        gst_carry = min(due, 0.0)
        scheduled("gst", d, int(round(max(due, 0.0))), "gst_payment", nr.gst_payment(rn(d), st, supplier_gstin))
        d = _last_day_of_month(date(y, m, 1))
        add_txn(d, -int(rn(d).integers(59, 591)) * 100, "bank_charges", nr.bank_charge(rn(d), st, d, quarterly=False))
        if m in (1, 4, 7, 10):
            d = date(y, m, 1)
            add_txn(d, -int(rn(d).integers(15, 60)) * 100, "bank_charges", nr.bank_charge(rn(d), st, d, quarterly=True))

    # Unscheduled flows (supplier payments are not here; they are purchases).
    rp, rf = streams("flows")
    d = c.start
    while d <= end:
        r, g = (rp, rn_past) if d <= t0 else (rf, rn_future)
        for _ in range(r.poisson(c.misc_credits_per_day)):
            amt = int(round(c.misc_credit_median_paise * math.exp(0.6 * r.normal())))
            if r.random() < 0.5:
                add_txn(d, amt, "refund", nr.upi_credit(g, st, suppliers[int(r.integers(len(suppliers)))], "REFUND"))
            else:
                add_txn(d, amt, "misc_income", nr.upi_credit(g, st, nr.person_name(g), "PAYMENT"))
        for _ in range(r.poisson(c.misc_expenses_per_day)):
            amt = int(round(c.misc_expense_median_paise * math.exp(0.6 * r.normal())))
            add_txn(d, -amt, "misc_expense", nr.upi_debit(g, st, nr.shop_name(g), "PAYMENT"))
        if r.random() < c.cash_withdrawals_per_week / 7:
            add_txn(d, -int(r.integers(1, 6)) * 10_000_00, "cash_withdrawal", nr.cash_withdrawal(g, st))
        if r.random() < c.owner_transfers_per_month / 30:
            add_txn(d, -int(r.integers(5, 21)) * 10_000_00, "owner_transfer", nr.owner_transfer(g, st, owner))
        d += timedelta(days=1)

    # Order, ids and known_at. Past-dated txns sort first, so their ids and
    # known_at draws never depend on the future.
    order = sorted(range(len(txns)), key=lambda i: (txns[i]["value_date"], i))
    uid_of = {}
    rk_past, rk_future = streams("known_at")
    rows = []
    day_seq = 0
    for n, i in enumerate(order):
        t = txns[i]
        rk = rk_past if t["value_date"] <= t0 else rk_future
        bank_txn_id = f"SYN{n:07d}"
        uid_of[i] = ids.txn_uid(c.tenant_id, c.account_id,
                                ids.txn_key_bank_id(bank_txn_id, t["value_date"], t["amount_paise"]))
        day_seq = day_seq + 1 if rows and rows[-1]["value_date"] == t["value_date"] else 0
        known_at = datetime.combine(t["value_date"], time(), tzinfo=timezone.utc) + timedelta(
            days=int(rk.integers(3)), seconds=int(rk.integers(6 * 3600, 23 * 3600)))
        rows.append({
            "tenant_id": c.tenant_id, "txn_uid": uid_of[i], "account_id": c.account_id,
            "value_date": t["value_date"], "known_at": known_at, "amount_paise": t["amount_paise"],
            "direction": "credit" if t["amount_paise"] > 0 else "debit",
            "narration_raw": t["narration_raw"], "source": "synth", "dq_score": 1.0, "dup_group_id": None,
            "category": t["category"], "rail": t["rail"], "process": PROCESS_OF_CATEGORY[t["category"]],
            "bank_txn_id": bank_txn_id, "day_seq": day_seq,
        })
    all_txns = pl.DataFrame(rows, schema={**TXN, "category": pl.String, "rail": pl.String, "process": pl.String,
                                          "bank_txn_id": pl.String, "day_seq": pl.Int32})
    opening = c.as_of_balance_paise - int(all_txns.filter(pl.col("value_date") <= t0)["amount_paise"].sum())

    # Contract invoice ids; the generator's invoice number stays in truth and narrations.
    id_of = {inv["invoice_id"]: ids.invoice_id(c.tenant_id, supplier_gstin, inv["invoice_id"],
                                               fiscal_year(inv["issue_date"])) for inv in invoices}
    remap = pl.col("invoice_id").replace_strict(id_of)

    pay_df = pl.DataFrame(
        [{**p, "txn_uid": uid_of.get(p["txn_idx"])} for p in payments],
        schema={"invoice_id": pl.String, "part": pl.Int32, "txn_idx": pl.Int64, "value_date": pl.Date,
                "amount_paise": pl.Int64, "tds_paise": pl.Int64, "payer_kind": pl.String, "txn_uid": pl.String},
    ).drop("txn_idx").with_columns(remap)

    purchases_df = pl.DataFrame(
        [{**p, "txn_uid": uid_of.get(p["txn_idx"])} for p in purchases],
        schema={"invoice_id": pl.String, "txn_idx": pl.Int64, "value_date": pl.Date,
                "amount_paise": pl.Int64, "lag_days": pl.Int32, "txn_uid": pl.String},
    ).drop("txn_idx").filter(pl.col("value_date").is_between(c.start, end)).with_columns(remap)

    inv_truth = pl.DataFrame(
        [{k: v for k, v in inv.items() if k != "buyer"} | {"buyer_id": inv["buyer"]["buyer_id"]} for inv in invoices],
        schema={"invoice_id": pl.String, "issue_date": pl.Date, "due_date": pl.Date, "terms_days": pl.Int32,
                "gross_paise": pl.Int64, "gst_paise": pl.Int64, "tds_paise": pl.Int64, "never_paid": pl.Boolean,
                "pay_week": pl.Int32, "first_paid_date": pl.Date, "paid_date": pl.Date, "part_paid": pl.Boolean,
                "terms_unknown": pl.Boolean, "treds_discount_date": pl.Date, "buyer_id": pl.String},
    ).filter(pl.col("issue_date").is_between(c.start, end)).with_columns(
        pl.col("invoice_id").alias("invoice_no"), remap, pl.col("terms_unknown").fill_null(False))

    buyers_truth = pl.DataFrame(
        [{k: b[k] for k in ("buyer_id", "type", "sector", "p", "p_early", "pi", "loading", "mix_share", "tds_rate")}
         for b in buyers]
    )

    obligations_df = pl.DataFrame(
        [{"tenant_id": c.tenant_id, **o} for o in obligations], schema=OBLIGATION,
    )

    observed = _contract_tables(c, all_txns, pay_df, inv_truth, buyers, opening)
    truth = Truth(
        config=c, past_seed=past_seed, future_seed=future_seed, opening_balance_paise=opening,
        sector_shocks=shocks, weekly_orders=pl.DataFrame(weekly), buyers=buyers_truth,
        invoices=inv_truth, payments=pay_df, purchases=purchases_df, transactions=all_txns,
        balances=_balances(all_txns, c, opening, end), obligations=obligations_df,
        buyer_records=[{k: v for k, v in b.items()} for b in buyers],
    )
    return observed, truth


def contract_tables_at(truth: Truth, cut: datetime) -> dict[str, pl.DataFrame]:
    """The contract tables Track A would hold at time cut, for any cut inside the world.

    generate_world's observed tables are contract_tables_at(truth, config.as_of).
    """
    return _contract_tables(truth.config, truth.transactions, truth.payments, truth.invoices, truth.buyer_records,
                            truth.opening_balance_paise, cut)


def complete_through(known_txns: pl.DataFrame, cut: datetime) -> date:
    """Per-account "ledger complete through" watermark (the rule proposed to Track A).

    L = the largest reporting lag seen so far, max(known_at date - value_date) over the
    account's known transactions; the ledger is taken as complete through cut date - L.
    A transaction value-dated on or before the watermark is assumed to be known by cut.
    With several accounts, the tenant's watermark is the earliest one.
    """
    if known_txns.is_empty():
        return cut.date()
    lag = known_txns.group_by("account_id").agg(
        (pl.col("known_at").dt.date() - pl.col("value_date")).dt.total_days().max().alias("lag"))
    return min(cut.date() - timedelta(days=int(l)) for l in lag["lag"])


def _utc_midnight(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=timezone.utc)


def _contract_tables(c: WorldConfig, all_txns: pl.DataFrame, pay_df: pl.DataFrame, inv_truth: pl.DataFrame,
                     buyers: list[dict], opening: int, cut: datetime | None = None) -> dict[str, pl.DataFrame]:
    """The tables Track A would hold at cut (default c.as_of), in contract shape: rows with known_at <= cut.

    Append-only versions: an invoice and its label get a row when issued (known at midnight UTC
    on the issue date, before any credit for it), one per observed payment (known when its credit is), and open labels
    are re-stamped at cut with censored_at = the ledger-complete-through watermark
    (complete_through). The bank-reported balance at cut is the true closing balance of
    cut's date, from every transaction value-dated by then, reported or not.
    """
    cut = cut or c.as_of
    t0 = cut.date()
    obs_txns = all_txns.filter(pl.col("known_at") <= cut)
    watermark = complete_through(obs_txns, cut)
    known_at = dict(zip(obs_txns["txn_uid"], obs_txns["known_at"]))
    pays = pay_df.filter(pl.col("txn_uid").is_in(list(known_at))).with_columns(
        pl.col("txn_uid").replace_strict(known_at, return_dtype=pl.Datetime("us", "UTC")).alias("known_at")
    ).sort("known_at")
    pays_by_inv: dict[str, list[dict]] = {}
    for r in pays.iter_rows(named=True):
        pays_by_inv.setdefault(r["invoice_id"], []).append(r)
    rate_of = {b["buyer_id"]: b["tds_rate"] for b in buyers}

    inv_rows, out_rows, match_rows = [], [], []
    for inv in inv_truth.filter(pl.col("issue_date") <= t0).sort("issue_date", "invoice_id").iter_rows(named=True):
        unknown = inv["terms_unknown"]
        base = {
            "tenant_id": c.tenant_id, "invoice_id": inv["invoice_id"],
            "irn": _irn(inv["invoice_no"]) if c.has_irn else None, "buyer_id": inv["buyer_id"],
            "issue_date": inv["issue_date"],
            "due_date": inv["issue_date"] + timedelta(days=45) if unknown else inv["due_date"],
            "terms_days": None if unknown else inv["terms_days"],
            "terms_source": "unknown" if unknown else "document_terms",
            "gross_paise": inv["gross_paise"], "gst_paise": inv["gst_paise"],
            "expected_tds_paise": inv["tds_paise"],
        }
        label = {"tenant_id": c.tenant_id, "invoice_id": inv["invoice_id"], "buyer_id": inv["buyer_id"],
                 "issue_date": inv["issue_date"]}
        issued = _utc_midnight(inv["issue_date"])
        inv_rows.append({**base, "status": "open", "known_at": issued})
        out_rows.append({**label, "first_paid_date": None, "paid_date": None, "paid_fraction": 0.0,
                         "settlement_kind": None, "censored_at": inv["issue_date"], "known_at": issued})
        net = inv["gross_paise"] - inv["tds_paise"]
        received, settled, first = 0, 0, None
        latest = out_rows[-1]
        for p in pays_by_inv.get(inv["invoice_id"], []):
            treds = p["payer_kind"] == "treds_financier"
            received += p["amount_paise"]
            settled += p["amount_paise"] + p["tds_paise"]
            first = first or p["value_date"]
            paid = treds or received >= net
            k = p["known_at"]
            match_rows.append({
                "tenant_id": c.tenant_id, "txn_uid": p["txn_uid"], "invoice_id": inv["invoice_id"],
                "allocated_paise": p["amount_paise"], "tds_inferred_paise": p["tds_paise"], "confidence": 1.0,
                "method": "reference", "payer_kind": p["payer_kind"], "tds_rate": rate_of[inv["buyer_id"]],
                "tds_basis": "taxable_value", "known_at": k,
            })
            inv_rows.append({**base, "status": "paid" if paid else "part_paid", "known_at": k})
            latest = {**label, "first_paid_date": first, "paid_date": p["value_date"] if paid else None,
                      "paid_fraction": 1.0 if treds else min(settled / inv["gross_paise"], 1.0),
                      "settlement_kind": ("treds_discount" if treds else "buyer") if paid else None,
                      "censored_at": p["value_date"] if treds else (None if paid else k.date()), "known_at": k}
            out_rows.append(latest)
        if latest["paid_date"] is None and latest["known_at"] < cut:
            out_rows.append({**latest, "censored_at": watermark, "known_at": cut})

    bal = _balances(obs_txns, c, opening, t0)
    ka = obs_txns.sort("value_date").group_by("value_date", maintain_order=True).agg(pl.col("known_at").max())
    bal = bal.join(ka.rename({"value_date": "date"}), on="date", how="left").sort("date").with_columns(
        pl.col("known_at").cum_max().fill_null(_utc_midnight(c.start - timedelta(days=1))))
    bal = bal.with_columns(  # keep (date, known_at) unique against the bank-reported row below
        pl.when(pl.col("known_at") == cut).then(pl.col("known_at") - timedelta(microseconds=1))
        .otherwise(pl.col("known_at")).alias("known_at"),
        pl.lit("reconstructed").alias("balance_source"))
    if c.bank_reported_balance:
        bank = pl.DataFrame({"tenant_id": [c.tenant_id], "account_id": [c.account_id], "date": [t0],
                             "closing_paise": [opening + int(all_txns.filter(pl.col("value_date") <= t0)
                                                             ["amount_paise"].sum())],
                             "continuity_ok": [None], "balance_source": ["bank_reported"], "known_at": [cut]})
        bal = pl.concat([bal.select(bank.columns), bank.cast(bal.select(bank.columns).schema)])

    def frame(table: str, rows) -> pl.DataFrame:
        sch = schema(table)
        df = rows if isinstance(rows, pl.DataFrame) else pl.DataFrame(rows, schema=sch, orient="row")
        return df.select(list(sch)).cast(sch)

    return {
        "ledger.transactions": frame("ledger.transactions", obs_txns),
        "ledger.balances": frame("ledger.balances", bal),
        "ledger.invoices": frame("ledger.invoices", inv_rows),
        "ledger.matches": frame("ledger.matches", match_rows),
        "labels.invoice_outcomes": frame("labels.invoice_outcomes", out_rows),
        "labels.txn_corrections": frame("labels.txn_corrections", []),  # owner corrections: none in synth
        "ref.buyers": frame("ref.buyers", [
            {"buyer_id": b["buyer_id"], "id_basis": "pan", "pan": b["pan"], "gstin": b["gstin"], "name": b["name"],
             "type": b["type"], "sector": b["sector"], "sector_source": "cin_nic" if b["type"] == "unlisted" else "exchange",
             "treds_mandated": True if b["type"] == "cpse" else None}  # VERIFY: CPSE TReDS mandate
            for b in buyers]),
        "ref.tenant_buyers": frame("ref.tenant_buyers", [
            {"tenant_id": c.tenant_id, "buyer_id": b["buyer_id"], "display_name": None, "owner_sector": None,
             "written_agreement": None, "treds_onboarded": None} for b in buyers]),
    }


def _conditional_geometric(r, issue: date, t0: date, end: date, pi: float, hazard):
    """Draw (never, pay_week, first date) given the invoice was not paid by t0.

    Survival to t0 for an eventual payer is S = prod over weeks wholly before
    t0 of (1 - h_k), times (1 - h_s f) for the week straddling t0, where f is
    the share of that week's days on or before t0 (pay day is uniform in the
    week). Then P(never | open at t0) = pi / (pi + (1 - pi) S).
    """
    k, survive = 1, 1.0
    straddle = None
    while (wk := issue + timedelta(days=7 * (k - 1))) <= t0:
        days_in = (t0 - wk).days + 1
        if days_in >= 7:
            survive *= 1 - hazard(k)
        else:
            straddle = (k, days_in)
            survive *= 1 - hazard(k) * days_in / 7
            break
        k += 1
    if r.random() < pi / (pi + (1 - pi) * survive):
        return True, None, None
    if straddle is not None:
        k, days_in = straddle
        h = hazard(k)
        if r.random() < h * (1 - days_in / 7) / (1 - h * days_in / 7):
            day = int(r.integers(days_in, 7))
            return False, k, issue + timedelta(days=7 * (k - 1) + day)
        k += 1
    while (wk := issue + timedelta(days=7 * (k - 1))) <= end:
        if r.random() < hazard(k):
            return False, k, wk + timedelta(days=int(r.integers(7)))
        k += 1
    return False, None, None


def _balances(txns: pl.DataFrame, c: WorldConfig, opening: int, last: date) -> pl.DataFrame:
    """Daily closing balance from an opening row on start - 1 day."""
    days = pl.DataFrame({"date": pl.date_range(c.start - timedelta(days=1), last, "1d", eager=True)})
    daily = txns.group_by(pl.col("value_date").alias("date")).agg(pl.col("amount_paise").sum().alias("flow"))
    return (
        days.join(daily, on="date", how="left")
        .with_columns(pl.col("flow").fill_null(0))
        .sort("date")
        .select(
            pl.lit(c.tenant_id).alias("tenant_id"),
            pl.lit(c.account_id).alias("account_id"),
            "date",
            (pl.lit(opening, pl.Int64) + pl.col("flow").cum_sum()).alias("closing_paise"),
            pl.lit(True).alias("continuity_ok"),
        )
        .cast(TRUTH_BALANCE)
    )


@dataclass
class Futures:
    """n truth futures sharing one past."""

    config: WorldConfig
    past_seed: int
    observed: dict[str, pl.DataFrame]
    truths: list[Truth]

    def balance_paths(self, horizon_days: int) -> np.ndarray:
        """(n_futures, horizon_days) true closing balances for days as_of+1 .. as_of+horizon."""
        if horizon_days > self.config.future_days:
            raise ValueError("horizon_days exceeds the simulated future")
        t0 = self.config.as_of_date
        return np.stack([
            t.balances.filter(pl.col("date").is_between(t0 + timedelta(days=1), t0 + timedelta(days=horizon_days)))
            ["closing_paise"].to_numpy()
            for t in self.truths
        ])

    def oracle_shortfall(self, threshold_paise: int | None = None, horizon_days: int = 90) -> pl.DataFrame:
        """True P(balance has gone below threshold by day t), t = 1..horizon."""
        threshold = self.config.shortfall_threshold_paise if threshold_paise is None else threshold_paise
        paths = self.balance_paths(horizon_days)
        ever_below = np.minimum.accumulate(paths, axis=1) < threshold
        t0 = self.config.as_of_date
        return pl.DataFrame({
            "day": np.arange(1, horizon_days + 1, dtype=np.int32),
            "date": [t0 + timedelta(days=d) for d in range(1, horizon_days + 1)],
            "p_short": ever_below.mean(axis=0),
        })


def resample_futures(config: WorldConfig, past_seed: int, n: int) -> Futures:
    worlds = [generate_world(config, past_seed, future_seed) for future_seed in range(n)]
    return Futures(config=config, past_seed=past_seed, observed=worlds[0][0], truths=[t for _, t in worlds])
