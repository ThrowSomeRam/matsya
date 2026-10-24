"""Payment-timing model: recovery, robustness and benchmark against the due-date assumption.

Run: python eval/models/timing_benchmark.py
"""

from matsya_intelligence.synth import WorldConfig
from matsya_intelligence.timing.evaluate import CAL_WEEKS, DUE_DATE_MASS, benchmark, recovery, rhat_check, robustness


def main() -> None:
    r = recovery(WorldConfig(shock_sigma=0.0, part_payment_prob=0.0), range(100))
    print("## Recovery (true type hyperprior, shocks off, 100 pasts)\n")
    print(f"buyers {r['n_buyers']}: 90% interval coverage p_early {r['coverage_p_early']:.3f}, "
          f"p {r['coverage_p']:.3f}, pi {r['coverage_pi']:.3f}; MAE p_early {r['mae_p_early']:.4f}, "
          f"p {r['mae_p']:.4f}, pi {r['mae_pi']:.4f}\n")

    h = rhat_check(WorldConfig(shock_sigma=0.0, part_payment_prob=0.0), range(10))
    print(f"## R-hat ({h['n_chains']} chains x {h['n_draws']} draws after {h['burn_in']} burn-in, "
          f"{h['n_buyers']} buyers)\n")
    for name in ("p_early", "p", "pi"):
        print(f"{name}: max {h[f'max_rhat_{name}']:.4f}, share < 1.01 {h[f'share_below_1.01_{name}']:.2f}")

    print("\n## Robustness (recovery under misspecification, 30 pasts each)\n")
    print("| knob | n buyers | cov p_early | cov p | cov pi | MAE p_early | MAE p | MAE pi |")
    print("|---|---|---|---|---|---|---|---|")
    for name, x in robustness(range(30)).items():
        print(f"| {name} | {x['n_buyers']} | {x['coverage_p_early']:.3f} | {x['coverage_p']:.3f} | "
              f"{x['coverage_pi']:.3f} | {x['mae_p_early']:.4f} | {x['mae_p']:.4f} | {x['mae_pi']:.4f} |")

    b = benchmark(range(30))
    print(f"\n## Benchmark vs due-date assumption ({b['n_worlds']} borderline worlds, shocks and part-payments on, "
          f"{b['n_invoices']} open invoices, horizon 13 weeks; due-date baseline puts {DUE_DATE_MASS} on its week)\n")
    print("| method | log score | " + " | ".join(f"Brier {h}w | ECE {h}w" for h in CAL_WEEKS) + " |")
    print("|---|---|" + "---|---|" * len(CAL_WEEKS))
    for m in ("model", "due_date"):
        x = b[m]
        print(f"| {m} | {x['log_score']:.3f} | " + " | ".join(
            f"{x[f'within_{h}w']['brier']:.3f} | {x[f'within_{h}w']['ece']:.3f}" for h in CAL_WEEKS) + " |")
    print("\nreliability, model, P(paid within 8 weeks): (mean predicted, observed, n)")
    print(b["model"]["within_8w"]["reliability"])


if __name__ == "__main__":
    main()
