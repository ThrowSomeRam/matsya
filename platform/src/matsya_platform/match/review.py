"""The review set as data an owner can work: ranked by the cost of getting each item wrong,
carrying the evidence the matcher computed, grouped where several items share one cause.

Risk of an item = at-risk rupees (the credit) x (1 + days the paid date would move /
risk_days_scale) x 1 / sqrt(1 + n), where n is how many invoices this buyer already has
labelled paid. A bad label on a buyer with three invoices distorts that buyer's estimate
far more than on one with forty. Days moved = from the credit's date to the date the
invoice would otherwise stay censored at.

Two candidates the matcher cannot tell apart are reported as tied, both at rank 1, never
ordered one first: a false ranking invites rubber-stamping.
"""

import math
from collections import Counter

TOP_N = (5, 10, 25)


def _buyer_stats(m) -> tuple[dict[str, float], dict[str, int]]:
    """Per buyer: share of accepted links that carried a reference; invoices labelled paid."""
    refs, links, paid = Counter(), Counter(), Counter()
    for inv_id, book in m.books.items():
        b = m.invoices[inv_id].buyer_id
        for e in book.entries:
            links[b] += 1
            refs[b] += e.expl.stage == "reference"
        if book.entries and not m._open(m.invoices[inv_id]):
            paid[b] += 1
    return {b: refs[b] / links[b] for b in links}, dict(paid)


def rank(m, invs: dict, credits: list, complete_through, cfg) -> tuple[list[dict], list[dict], dict]:
    by_uid = {c.txn_uid: c for c in credits}
    ref_share, paid = _buyer_stats(m)
    items = []
    for it in m.review:
        c = by_uid[it.txn_uid]
        n = paid.get(it.buyer_id, 0) if it.buyer_id else 0
        days_moved = max(0, (complete_through - it.value_date).days)
        risk = it.amount_paise * (1 + days_moved / cfg.risk_days_scale) / math.sqrt(1 + n)
        cands = list(it.candidates)
        tied = len(cands) >= 2 and cands[1].cost - cands[0].cost <= cfg.min_margin_paise
        top_cost = cands[0].cost if cands else None
        evidence = []
        for k, e in enumerate(cands):
            inv0 = invs[e.invoice_ids[0]]
            habit = m.habits.get(inv0.buyer_id)
            evidence.append({
                "rank": 1 if tied and e.cost - top_cost <= cfg.min_margin_paise else k + 1,
                "invoice_ids": list(e.invoice_ids), "stage": e.stage, "unexplained_paise": e.unexplained_paise,
                "tds_rate": e.hyp[0] if e.hyp else None, "tds_basis": e.hyp[1] if e.hyp else None,
                "days_vs_due": (it.value_date - inv0.due_date).days, "basis_signal": e.basis_signal,
                "habit_match": None if habit is None or e.hyp is None else e.hyp == habit,
            })
        items.append({
            "txn_uid": it.txn_uid, "value_date": it.value_date, "amount_paise": it.amount_paise,
            "buyer_id": it.buyer_id, "reason": it.reason, "cause": it.reason.split(":")[0],
            "credit_has_reference": bool(c.refs),
            "buyer_reference_share": ref_share.get(it.buyer_id) if it.buyer_id else None,
            "unusual_missing_reference": (not c.refs) and ref_share.get(it.buyer_id, 0.0) >= 0.5,
            "buyer_paid_invoices": n, "days_moved": days_moved, "risk": round(risk, 2),
            "indistinguishable": tied, "candidates": evidence,
        })
    items.sort(key=lambda x: (-x["risk"], x["txn_uid"]))
    for k, x in enumerate(items, start=1):
        x["position"] = k
    groups: dict[tuple, dict] = {}
    for x in items:
        g = groups.setdefault((x["buyer_id"], x["cause"]), {"buyer_id": x["buyer_id"], "cause": x["cause"],
                                                             "items": [], "risk": 0.0, "at_risk_paise": 0})
        g["items"].append(x["txn_uid"])
        g["risk"] += x["risk"]
        g["at_risk_paise"] += x["amount_paise"]
    group_list = sorted(groups.values(), key=lambda g: (-g["risk"], g["buyer_id"] or "", g["cause"]))
    for g in group_list:
        g["one_decision"] = len(g["items"]) > 1
        g["risk"] = round(g["risk"], 2)
    total = sum(x["amount_paise"] for x in items)
    coverage = {f"top_{n}": (sum(x["amount_paise"] for x in items[:n]) / total if total else 1.0) for n in TOP_N}
    coverage["items"] = len(items)
    coverage["at_risk_paise"] = total
    return items, group_list, coverage
