"""Simulation engine against the oracle, the gap-1 check, and speed.

Run: python eval/models/engine_benchmark.py
"""

from matsya_intelligence.engine.evaluate import gap1, learned_vs_oracle, speed, true_vs_oracle


def main() -> None:
    print("## True-parameter mode vs oracle (200 futures, 10k paths)\n")
    print("| past | day | engine | oracle | diff | MC se | |diff|/se |")
    print("|---|---|---|---|---|---|---|")
    for r in true_vs_oracle():
        d = r["engine"] - r["oracle"]
        print(f"| {r['seed']} | {r['day']} | {r['engine']:.3f} | {r['oracle']:.3f} | {d:+.3f} | {r['se']:.3f} | {abs(d) / r['se']:.2f} |")

    lv = learned_vs_oracle(range(50))
    print(f"\n## Learned mode vs oracle (50 borderline pasts, oracle from {lv['n_futures']} futures)\n")
    print(f"mean gap p_short(90) {lv['mean_gap_90']:+.3f}, mean |gap| {lv['mean_abs_gap_90']:.3f}\n")
    print("| bin of predicted p_short(90) | n | mean predicted | mean oracle |")
    print("|---|---|---|---|")
    for r in lv["calibration_90"]:
        print(f"| {r['bin']} | {r['n']} | {r['mean_predicted']:.3f} | {r['mean_oracle']:.3f} |")
    print("\nper past (first 10): seed, engine 30/60/90, oracle 30/60/90")
    for r in lv["per_seed"][:10]:
        print(r["seed"], [round(r[f"engine_{d}"], 3) for d in (30, 60, 90)], [round(r[f"oracle_{d}"], 3) for d in (30, 60, 90)])

    g = gap1(range(20))
    print("\n## Gap-1: median balance minus oracle median (Rs), mean over 20 pasts\n")
    print("| horizon | with future invoices | without |")
    print("|---|---|---|")
    for h in g["with_future_invoices"]:
        print(f"| {h} | {g['with_future_invoices'][h]:,.0f} | {g['without_future_invoices'][h]:,.0f} |")

    print("\n## Speed\n")
    print(speed())


if __name__ == "__main__":
    main()
