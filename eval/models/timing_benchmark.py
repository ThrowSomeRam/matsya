"""Payment-timing model: recovery, robustness and benchmark against the due-date assumption.

Run: python eval/models/timing_benchmark.py
"""

from matsya_intelligence.synth import WorldConfig
from matsya_intelligence.timing.evaluate import CAL_WEEKS, DUE_DATE_MASS, benchmark, recovery, robustness


def main() -> None:
    r = recovery(WorldConfig(shock_sigma=0.0, part_payment_prob=0.0), range(100))
    print("## Recovery (true type hyperprior, shocks off, 100 pasts)\n")
    print(f"buyers {r['n_buyers']}: 90% interval coverage p {r['coverage_p']:.3f}, pi {r['coverage_pi']:.3f}; "
          f"MAE p {r['mae_p']:.4f}, pi {r['mae_pi']:.4f}\n")

    print("## Robustness (recovery under misspecification, 30 pasts each)\n")
    print("| knob | n buyers | coverage p | coverage pi | MAE p | MAE pi |")
    print("|---|---|---|---|---|---|")
    for name, x in robustness(range(30)).items():
        print(f"| {name} | {x['n_buyers']} | {x['coverage_p']:.3f} | {x['coverage_pi']:.3f} | "
              f"{x['mae_p']:.4f} | {x['mae_pi']:.4f} |")

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
