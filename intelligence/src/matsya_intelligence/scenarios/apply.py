"""Apply a transform list to a copy of the simulation engine's inputs.

The input EngineInputs and everything it holds (posterior draws, fitted sales model,
obligations, features) are never modified: the result is a new EngineInputs that shares
them and carries the changes as a new fixed-obligations list and ScenarioEffects.

Several transforms compose: multipliers multiply, shifts add, the last terms change for a
buyer wins, the longest purchase stickiness wins, cash events and freezes accumulate. Dates become days after as_of.
"""

from dataclasses import replace

import numpy as np

from matsya_intelligence.engine.core import EngineInputs, ScenarioEffects


def _day(inp: EngineInputs, d) -> int:
    return (d - inp.as_of).days


def _buyers(inp: EngineInputs, ids: list[str]) -> np.ndarray:
    unknown = set(ids) - set(inp.buyer_ids)
    if unknown:
        raise ValueError(f"unknown buyers {sorted(unknown)}")
    return np.array([b in ids for b in inp.buyer_ids])


def apply_transforms(inp: EngineInputs, transforms: list) -> EngineInputs:
    n_b = len(inp.buyer_ids)
    pay_mult = np.ones(n_b)
    due_week = np.full(n_b, -1, dtype=np.int64)
    order_mult, purchase_shift, sticky = 1.0, 0, 0
    fixed = list(inp.fixed)
    cash, freezes, windows, inv_cash, partial = [], [], [], [], []
    for t in transforms:
        if t.type == "rate_multiplier" and t.weeks is not None:
            if t.target == "all_buyers":
                mask = np.ones(n_b, dtype=bool)
            elif t.target == "buyer":
                mask = _buyers(inp, t.ids)
            else:
                mask = np.array([s in t.ids for s in inp.buyer_sectors], dtype=bool)
            windows.append((mask, t.factor, 7 * t.weeks))
        elif t.type == "rate_multiplier":
            if t.target == "orders":
                order_mult *= t.factor
                sticky = max(sticky, t.stickiness_days)
            elif t.target == "all_buyers":
                pay_mult *= t.factor
            elif t.target == "buyer":
                pay_mult[_buyers(inp, t.ids)] *= t.factor
            else:
                if not inp.buyer_sectors:
                    raise ValueError("inputs carry no buyer sectors")
                unknown = set(t.ids) - set(inp.buyer_sectors)
                if unknown:
                    raise ValueError(f"no buyers in sectors {sorted(unknown)}")
                pay_mult[np.array([s in t.ids for s in inp.buyer_sectors])] *= t.factor
        elif t.type == "date_shift":
            if t.target == "supplier_payments":
                if t.share is None:
                    purchase_shift += t.days
                else:
                    partial.append((t.share, t.days))
                continue
            keys = inp.fixed_kinds if t.target == "obligation_kind" else inp.fixed_ids
            if len(keys) != len(fixed):
                raise ValueError("inputs carry no obligation kinds/ids for the fixed obligations")
            unknown = set(t.ids) - set(keys)
            if unknown:
                raise ValueError(f"no fixed obligations for {sorted(unknown)}")
            fixed = [(max(d + t.days, 1), a) if k in t.ids else (d, a) for (d, a), k in zip(fixed, keys)]
        elif t.type == "cash_event":
            first = _day(inp, t.on)
            last = first if t.window_end is None else _day(inp, t.window_end)
            if first < 1:
                raise ValueError(f"cash event on {t.on} is not after as_of {inp.as_of}")
            cash.append((first, last, t.amount_paise))
        elif t.type == "terms_change":
            if t.party == "supplier":
                purchase_shift += t.to_days - t.from_days
            else:
                sel = _buyers(inp, t.ids) if t.ids else np.ones(n_b, dtype=bool)
                due_week[sel] = t.to_days // 7 + 1
        elif t.type == "freeze":
            freezes.append((max(_day(inp, t.start), 1), t.days, t.fraction))
        elif t.type == "invoice_cash":
            if t.invoice_id not in inp.open_invoice_ids:
                raise ValueError(f"{t.invoice_id} is not an open invoice")
            if _day(inp, t.on) < 1:
                raise ValueError(f"invoice cash on {t.on} is not after as_of {inp.as_of}")
            inv_cash.append((inp.open_invoice_ids.index(t.invoice_id), _day(inp, t.on), t.amount_paise, t.mode))
        else:
            raise ValueError(f"unknown transform {t.type!r}")
    effects = ScenarioEffects(
        pay_mult=None if (pay_mult == 1).all() else pay_mult,
        due_week=None if (due_week < 0).all() else due_week,
        order_mult=order_mult, purchase_sticky_days=sticky, purchase_shift_days=purchase_shift, cash=tuple(cash), freezes=tuple(freezes),
        pay_windows=tuple(windows), invoice_cash=tuple(inv_cash), purchase_partial_shifts=tuple(partial))
    if sum(s for s, _ in partial) > 1 + 1e-12:
        raise ValueError("partial supplier shifts cover more than every supplier payment")
    return replace(inp, fixed=fixed, scenario=effects)
