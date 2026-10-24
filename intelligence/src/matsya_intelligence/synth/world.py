"""Simulate one supplier's books end to end, then cut them at as_of.

The full timeline (start to as_of + future_days) is simulated first and kept
in Truth. Observed tables are what Track A would hand us at as_of: only rows
with known_at <= as_of, open invoices censored.
"""

import calendar
import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from matsya_intelligence.io.types import BALANCE, BUYER, INVOICE, INVOICE_OUTCOME, OBLIGATION, TXN
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth import narration as nr
from matsya_intelligence.synth.config import WorldConfig

# Which generating process each category comes from. Each process is forecast
# by exactly one component: sales -> sales forecaster + payment-timing model,
# purchase -> sales forecaster (linked purchases), fixed_obligation ->
# obligations detector, unscheduled -> flow forecaster.
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
}

CONSTANTS_PATH = Path(__file__).resolve().parents[3] / "config" / "track_b_constants.yaml"


@cache
def load_constants() -> dict:
    return yaml.safe_load(CONSTANTS_PATH.read_text())


@dataclass
class Truth:
    config: WorldConfig
    seed: int
    sector_shocks: pl.DataFrame  # sector, week, week_start, z
    weekly_orders: pl.DataFrame  # week, week_start, demand_x, mean, count
    buyers: pl.DataFrame  # planted p, pi, loading, mix_share, tds_rate per buyer
    invoices: pl.DataFrame  # every invoice with never_paid, pay_week, paid dates
    payments: pl.DataFrame  # one row per receipt: invoice_id, part, txn_uid, value_date, amounts
    purchases: pl.DataFrame  # one row per invoice: invoice_id, txn_uid, value_date, amount_paise, lag_days
    transactions: pl.DataFrame  # every txn incl. future, Txn columns + category + rail + process
    balances: pl.DataFrame  # daily closing from all txns
    obligations: pl.DataFrame  # every scheduled outflow, Obligation schema


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


def generate_world(config: WorldConfig, seed: int) -> tuple[dict[str, pl.DataFrame], Truth]:
    c = config
    run_id = f"synth-{seed}"
    consts = load_constants()
    tds_rates = consts["tds"]["matcher_candidate_rates"]
    cal = consts["statutory_calendar"]["monthly"]
    end = c.as_of_date + timedelta(days=c.future_days)
    n_weeks = (end - c.start).days // 7 + 2
    rn = rng_for(run_id, "narration")

    # Sector shocks: stationary AR(1), one value per world week.
    r = rng_for(run_id, "sectors")
    u = r.normal(size=(len(c.sectors), n_weeks))
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
    r = rng_for(run_id, "demand")
    common = u.mean(axis=0) * math.sqrt(len(c.sectors))
    e = c.demand_shock_corr * common + math.sqrt(1 - c.demand_shock_corr**2) * r.normal(size=n_weeks)
    var_x = c.demand_sigma**2 / (1 - c.demand_rho**2)
    x = np.zeros(n_weeks)
    x[0] = math.sqrt(var_x) * e[0]
    for t in range(1, n_weeks):
        x[t] = c.demand_rho * x[t - 1] + c.demand_sigma * e[t]
    order_mean = c.order_mean_per_week * np.exp(x - var_x / 2)

    # Buyers drawn from type-level hyperpriors; fixed shares of the order flow.
    r = rng_for(run_id, "buyers")
    types = list(c.type_priors)
    shares = np.array([c.type_priors[t].share for t in types])
    buyers = []
    for i in range(c.n_buyers):
        btype = str(r.choice(types, p=shares / shares.sum()))
        sector_idx = int(r.integers(len(c.sectors)))
        tp = c.type_priors[btype]
        pan = nr.make_pan(r)
        buyers.append({
            "buyer_id": f"B{i:03d}",
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
    for b, share in zip(buyers, mix):
        b["mix_share"] = float(share)

    # Invoices: NegBin weekly count (gamma-Poisson), buyer by fixed shares,
    # lognormal amounts.
    r = rng_for(run_id, "invoices")
    invoices = []
    weekly = []
    for w in range(n_weeks):
        week_start = c.start + timedelta(days=7 * w)
        if week_start > end:
            break
        lam = r.gamma(c.order_dispersion, order_mean[w] / c.order_dispersion)
        count = int(r.poisson(lam))
        weekly.append({"week": w, "week_start": week_start, "demand_x": float(x[w]),
                       "mean": float(order_mean[w]), "count": count})
        for _ in range(count):
            b = buyers[int(r.choice(c.n_buyers, p=mix))]
            issue = week_start + timedelta(days=int(r.integers(7)))
            gross = int(round(c.amount_median_paise * math.exp(c.amount_sigma * r.normal())))
            terms = int(r.choice(c.terms_choices))
            if issue > end:
                continue
            gst = int(round(gross * c.gst_rate / (1 + c.gst_rate)))
            inv_id = f"INV-{len(invoices):05d}"
            invoices.append({
                "invoice_id": inv_id,
                "buyer": b,
                "issue_date": issue,
                "due_date": issue + timedelta(days=terms),
                "terms_days": terms,
                "gross_paise": gross,
                "gst_paise": gst,
                "tds_paise": int(round(b["tds_rate"] * (gross - gst))),
            })

    txns: list[dict] = []

    def add_txn(d: date, amount: int, category: str, rail_narr: tuple[str, str]) -> int | None:
        if d < c.start or d > end or amount == 0:
            return None
        txns.append({"value_date": d, "amount_paise": amount, "category": category,
                     "rail": rail_narr[0], "narration_raw": rail_narr[1]})
        return len(txns) - 1

    # Purchases: ratio x sales, paid a planted lag before the invoice date.
    r = rng_for(run_id, "purchases")
    suppliers = [nr.company_name(rn, str(rn.choice(c.sectors)), "unlisted") for _ in range(8)]
    purchases = []
    for inv in invoices:
        lag = c.purchase_lag_days + int(r.integers(-c.purchase_lag_jitter_days, c.purchase_lag_jitter_days + 1))
        d = inv["issue_date"] - timedelta(days=lag)
        amount = int(round(c.purchase_ratio * inv["gross_paise"]))
        name = suppliers[int(r.integers(len(suppliers)))]
        idx = add_txn(d, -amount, "supplier_payment", nr.debit_to_party(rn, name, d, f"BILL {r.integers(100, 9999)}", amount))
        purchases.append({"invoice_id": inv["invoice_id"], "txn_idx": idx, "value_date": d,
                          "amount_paise": amount, "lag_days": lag})

    # Payment timing: weekly hazard p shifted by the sector shock, cure fraction pi.
    r = rng_for(run_id, "payments")
    bc = c.behaviour_change
    payments = []
    for inv in invoices:
        b, issue = inv["buyer"], inv["issue_date"]
        changed = bc is not None and bc.buyer_index == b["index"]
        pi = b["pi"]
        if changed and issue >= bc.at:
            pi = min(pi + bc.pi_add, 0.99)

        def p_at(d: date) -> float:
            p = b["p"]
            if changed and d >= bc.at:
                p = min(max(p * bc.p_multiplier, 1e-4), 1 - 1e-4)
            w = min((d - c.start).days // 7, n_weeks - 1)
            return _sigmoid(_logit(p) + b["loading"] * z[b["sector_idx"], w])

        never = bool(r.random() < pi)
        pay_week = None
        if not never:
            if c.lateness == "geometric":
                k = 1
                while issue + timedelta(days=7 * (k - 1)) <= end:
                    if r.random() < p_at(issue + timedelta(days=7 * (k - 1))):
                        pay_week = k
                        break
                    k += 1
            elif c.lateness == "lognormal":
                median_days = 7 / p_at(issue)
                days = median_days * math.exp(c.lognormal_sigma * r.normal())
                pay_week = max(1, math.ceil(days / 7))
            else:
                raise ValueError(f"unknown lateness {c.lateness!r}")

        inv.update(never_paid=never, pay_week=pay_week, first_paid_date=None, paid_date=None, part_paid=False)
        if pay_week is None:
            continue
        first = issue + timedelta(days=7 * (pay_week - 1) + int(r.integers(7)))
        if c.month_end_clustering and r.random() < c.month_end_prob:
            first = _last_day_of_month(first)
        net = inv["gross_paise"] - inv["tds_paise"]
        parts = [(first, net, inv["tds_paise"])]
        if r.random() < c.part_payment_prob:
            f = r.uniform(0.3, 0.7)
            second = first + timedelta(days=int(r.integers(7, 29)))
            n1, t1 = int(round(net * f)), int(round(inv["tds_paise"] * f))
            parts = [(first, n1, t1), (second, net - n1, inv["tds_paise"] - t1)]
            inv["part_paid"] = True
        inv["first_paid_date"], inv["paid_date"] = parts[0][0], parts[-1][0]
        ref = inv["invoice_id"].replace("-", " ") if r.random() < 0.7 else "PAYMENT"
        for part_no, (d, amount, tds) in enumerate(parts, start=1):
            if b["type"] == "cpse":
                rail_narr = nr.credit_via_treds(rn, d, ref)
            else:
                rail_narr = nr.credit_from_company(rn, b["name"], d, ref, amount)
            idx = add_txn(d, amount, "customer_receipt", rail_narr)
            payments.append({"invoice_id": inv["invoice_id"], "part": part_no, "txn_idx": idx,
                             "value_date": d, "amount_paise": amount, "tds_paise": tds})

    # Scheduled outflows. Each is both a txn and a truth obligation.
    obligations = []
    supplier_gstin = nr.make_gstin(rn, nr.make_pan(rn))
    landlord, owner, lender = nr.person_name(rn), nr.person_name(rn), str(rn.choice(nr.LENDERS))
    ca_firm = f"{nr.person_name(rn)} AND CO"
    tds_deducted: dict[tuple[int, int], int] = {}
    gst_by_month: dict[tuple[int, int], int] = {}
    for inv in invoices:
        key = (inv["issue_date"].year, inv["issue_date"].month)
        gst_by_month[key] = gst_by_month.get(key, 0) + inv["gst_paise"]

    def scheduled(kind: str, d: date, amount: int, category: str, rail_narr: tuple[str, str]):
        if add_txn(d, -amount, category, rail_narr) is not None:
            obligations.append({"obligation_id": f"OB-{kind}-{d:%Y%m}", "kind": kind,
                                "due_date": d, "amount_paise": amount, "confidence": 1.0})

    for y, m in _months(c.start, end):
        py, pm = _prev_month(y, m)
        d = date(y, m, c.salary_day)
        scheduled("salary", d, c.salary_paise, "salary", nr.salary(rn, d))
        d = date(y, m, c.professional_fee_day)
        tds = int(round(0.10 * c.professional_fee_paise))  # 194J professional fees
        scheduled("professional_fee", d, c.professional_fee_paise - tds, "professional_fee",
                  nr.debit_to_party(rn, ca_firm, d, "PROF FEES", c.professional_fee_paise))
        if c.start <= d <= end:
            tds_deducted[(y, m)] = tds
        d = date(y, m, c.rent_day)
        scheduled("rent", d, c.rent_paise, "rent", nr.debit_to_party(rn, landlord, d, "RENT", c.rent_paise))
        d = date(y, m, c.emi_day)
        scheduled("emi", d, c.emi_paise, "emi", nr.ach_emi(rn, lender))
        # TDS for the previous month: 7th, except March deductions are due 30 April.
        d = date(y, 4, 30) if pm == 3 else date(y, m, cal["tds_deposit"]["day"])
        scheduled("tds", d, tds_deducted.get((py, pm), 0), "tds_payment", nr.tds_payment(rn, d))
        d = date(y, m, cal["pf_deposit"]["day"])
        scheduled("pf", d, int(round(c.pf_rate * c.salary_paise)), "pf_esi", nr.pf_payment(rn))
        d = date(y, m, cal["esi_deposit"]["day"])
        scheduled("esi", d, int(round(c.esi_rate * c.salary_paise)), "pf_esi", nr.esi_payment(rn))
        d = date(y, m, cal["gstr3b_monthly"]["day"])
        if d.weekday() == 6:  # Sunday moves to next working day
            d += timedelta(days=1)
        gst = int(round(c.gst_net_of_itc * gst_by_month.get((py, pm), 0)))
        scheduled("gst", d, gst, "gst_payment", nr.gst_payment(rn, supplier_gstin))
        d = _last_day_of_month(date(y, m, 1))
        add_txn(d, -int(rn.integers(59, 591)) * 100, "bank_charges", nr.bank_charge(rn, d, quarterly=False))
        if m in (1, 4, 7, 10):
            d = date(y, m, 1)
            add_txn(d, -int(rn.integers(15, 60)) * 100, "bank_charges", nr.bank_charge(rn, d, quarterly=True))

    # Unscheduled flows (supplier payments are not here; they are purchases).
    r = rng_for(run_id, "flows")
    d = c.start
    while d <= end:
        for _ in range(r.poisson(c.misc_credits_per_day)):
            amt = int(round(c.misc_credit_median_paise * math.exp(0.6 * r.normal())))
            if r.random() < 0.5:
                add_txn(d, amt, "refund", nr.upi_credit(rn, suppliers[int(r.integers(len(suppliers)))], "REFUND"))
            else:
                add_txn(d, amt, "misc_income", nr.upi_credit(rn, nr.person_name(rn), "PAYMENT"))
        if r.random() < c.cash_withdrawals_per_week / 7:
            add_txn(d, -int(r.integers(1, 6)) * 10_000_00, "cash_withdrawal", nr.cash_withdrawal(rn))
        if r.random() < c.owner_transfers_per_month / 30:
            add_txn(d, -int(r.integers(5, 21)) * 10_000_00, "owner_transfer", nr.owner_transfer(rn, owner))
        d += timedelta(days=1)

    # Order, ids and known_at.
    order = sorted(range(len(txns)), key=lambda i: (txns[i]["value_date"], i))
    uid_of = {}
    rk = rng_for(run_id, "known_at")
    rows = []
    for n, i in enumerate(order):
        t = txns[i]
        uid_of[i] = f"{c.tenant_id}-T{n:07d}"
        known_at = datetime.combine(t["value_date"], time(), tzinfo=timezone.utc) + timedelta(
            days=int(rk.integers(3)), seconds=int(rk.integers(6 * 3600, 23 * 3600)))
        rows.append({
            "tenant_id": c.tenant_id, "txn_uid": uid_of[i], "account_id": c.account_id,
            "value_date": t["value_date"], "known_at": known_at, "amount_paise": t["amount_paise"],
            "direction": "credit" if t["amount_paise"] > 0 else "debit",
            "narration_raw": t["narration_raw"], "source": "synth", "dq_score": 1.0, "dup_group_id": None,
            "category": t["category"], "rail": t["rail"], "process": PROCESS_OF_CATEGORY[t["category"]],
        })
    all_txns = pl.DataFrame(rows, schema={**TXN, "category": pl.String, "rail": pl.String, "process": pl.String})

    pay_df = pl.DataFrame(
        [{**p, "txn_uid": uid_of.get(p["txn_idx"])} for p in payments],
        schema={"invoice_id": pl.String, "part": pl.Int32, "txn_idx": pl.Int64, "value_date": pl.Date,
                "amount_paise": pl.Int64, "tds_paise": pl.Int64, "txn_uid": pl.String},
    ).drop("txn_idx")

    purchases_df = pl.DataFrame(
        [{**p, "txn_uid": uid_of.get(p["txn_idx"])} for p in purchases],
        schema={"invoice_id": pl.String, "txn_idx": pl.Int64, "value_date": pl.Date,
                "amount_paise": pl.Int64, "lag_days": pl.Int32, "txn_uid": pl.String},
    ).drop("txn_idx")

    inv_truth = pl.DataFrame(
        [{k: v for k, v in inv.items() if k != "buyer"} | {"buyer_id": inv["buyer"]["buyer_id"]} for inv in invoices],
        schema={"invoice_id": pl.String, "issue_date": pl.Date, "due_date": pl.Date, "terms_days": pl.Int32,
                "gross_paise": pl.Int64, "gst_paise": pl.Int64, "tds_paise": pl.Int64, "never_paid": pl.Boolean,
                "pay_week": pl.Int32, "first_paid_date": pl.Date, "paid_date": pl.Date, "part_paid": pl.Boolean,
                "buyer_id": pl.String},
    )

    buyers_truth = pl.DataFrame(
        [{k: b[k] for k in ("buyer_id", "type", "sector", "p", "pi", "loading", "mix_share", "tds_rate")}
         for b in buyers]
    )
    buyers_obs = pl.DataFrame(
        [{"tenant_id": c.tenant_id, **{k: b[k] for k in ("buyer_id", "gstin", "pan", "name", "type", "sector")}}
         for b in buyers],
        schema=BUYER,
    )

    obligations_df = pl.DataFrame(
        [{"tenant_id": c.tenant_id, **o} for o in obligations], schema=OBLIGATION,
    )

    # As-of cut.
    obs_txns = all_txns.filter(pl.col("known_at") <= c.as_of)
    observed_uids = set(obs_txns["txn_uid"])

    obs_pay = pay_df.filter(pl.col("txn_uid").is_in(observed_uids)).group_by("invoice_id").agg(
        pl.col("amount_paise").sum().alias("received"), pl.col("value_date").max().alias("last_paid"))
    inv_obs = (
        inv_truth.filter(pl.col("issue_date") <= c.as_of_date)
        .join(obs_pay, on="invoice_id", how="left")
        .with_columns(
            pl.col("received").fill_null(0),
            (pl.col("gross_paise") - pl.col("tds_paise")).alias("net"),
        )
        .with_columns(
            pl.when(pl.col("received") >= pl.col("net")).then(pl.lit("paid"))
            .when(pl.col("received") > 0).then(pl.lit("part_paid"))
            .otherwise(pl.lit("open")).alias("status"),
        )
    )
    invoices_obs = inv_obs.select(
        pl.lit(c.tenant_id).alias("tenant_id"),
        "invoice_id",
        (pl.col("invoice_id").map_elements(_irn, return_dtype=pl.String) if c.has_irn
         else pl.lit(None, pl.String)).alias("irn"),
        "buyer_id", "issue_date", "due_date", "terms_days", "gross_paise", "gst_paise",
        pl.col("tds_paise").alias("expected_tds_paise"),
        "status",
    ).cast(INVOICE)
    outcomes_obs = inv_obs.select(
        pl.lit(c.tenant_id).alias("tenant_id"),
        "invoice_id", "buyer_id", "issue_date",
        pl.when(pl.col("status") == "paid").then(pl.col("last_paid")).alias("paid_date"),
        (pl.col("received") / pl.col("net")).clip(0.0, 1.0).alias("paid_fraction"),
        pl.when(pl.col("status") != "paid").then(pl.lit(c.as_of_date)).alias("censored_at"),
    ).cast(INVOICE_OUTCOME)

    observed = {
        "transactions": obs_txns.select(list(TXN)),
        "balances": _balances(obs_txns, c, c.as_of_date),
        "invoices": invoices_obs,
        "invoice_outcomes": outcomes_obs,
        "buyers": buyers_obs,
    }
    truth = Truth(
        config=c, seed=seed, sector_shocks=shocks, weekly_orders=pl.DataFrame(weekly), buyers=buyers_truth,
        invoices=inv_truth, payments=pay_df, purchases=purchases_df, transactions=all_txns, balances=_balances(all_txns, c, end),
        obligations=obligations_df,
    )
    return observed, truth


def _irn(invoice_id: str) -> str:
    return hashlib.sha256(invoice_id.encode()).hexdigest()


def _balances(txns: pl.DataFrame, c: WorldConfig, last: date) -> pl.DataFrame:
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
            (pl.lit(c.opening_balance_paise, pl.Int64) + pl.col("flow").cum_sum()).alias("closing_paise"),
            pl.lit(True).alias("continuity_ok"),
        )
        .cast(BALANCE)
    )
