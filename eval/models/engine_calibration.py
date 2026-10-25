"""Learned-mode calibration against the oracle, one fix at a time, plus the gap-1 variants.

Run: python eval/models/engine_calibration.py ORACLE_CACHE.pkl
The cache comes from engine.evaluate.oracle_summary over past seeds 0..99 with
200 futures each (it does not depend on the engine, so it is computed once).
"""

import pickle
import sys

from matsya_intelligence.engine.evaluate import learned_calibration, print_calibration

OFF = dict(sector_shocks=False, full_unscheduled=False, shock_base=False)
STEPS = [
    ("0. before (z = 0, 8-week unscheduled)", {}),
    ("1. + sector shocks", {"sector_shocks": True}),
    ("2. + full-history unscheduled bootstrap", {"full_unscheduled": True}),
    ("3. + base logit under shocks (no double counting)", {"shock_base": True}),
]


def main(cache: str) -> None:
    oracle = pickle.load(open(cache, "rb"))
    flags = dict(OFF)
    for title, change in STEPS:
        flags.update(change)
        print(f"\n## {title}\n")
        print_calibration(learned_calibration(oracle, **flags))

    print("\n## Gap-1: median balance minus oracle median (Rs), all fixes on\n")
    variants = {
        "with future invoices": {},
        "without future invoices or purchases": {"with_sales": False},
        "naive: no future invoices, purchases as flat outflow": {"with_sales": False, "naive_purchases": True},
    }
    res = {name: learned_calibration(oracle, **f)["median_gap_rs"] for name, f in variants.items()}
    print("| horizon | " + " | ".join(res) + " |")
    print("|---|" + "---|" * len(res))
    for h in next(iter(res.values())):
        print(f"| {h} | " + " | ".join(f"{r[h]:,.0f}" for r in res.values()) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
