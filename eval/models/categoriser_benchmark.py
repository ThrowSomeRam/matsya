"""Categoriser benchmark on synthetic worlds with template hold-out.

Run: python eval/models/categoriser_benchmark.py
Prints macro-F1, abstain share and n per method and test set, then per-class F1.
"""

from matsya_intelligence.categorise.evaluate import run


def main() -> None:
    r = run()
    print(f"train transactions: {r['n_train']}\n")
    print("| test set | method | macro-F1 | abstain | n |")
    print("|---|---|---|---|---|")
    for test in ("in_template", "held_out"):
        for method, s in r[test].items():
            print(f"| {test} | {method} | {s['macro_f1']:.3f} | {s['abstain_share']:.3f} | {s['n']} |")
    for test in ("in_template", "held_out"):
        print(f"\nper-class F1, {test}:")
        rows = {m: r[test][m]["per_class"] for m in r[test]}
        classes = sorted(rows["pipeline"])
        print("| class | " + " | ".join(rows) + " |")
        print("|---|" + "---|" * len(rows))
        for c in classes:
            print(f"| {c} | " + " | ".join(f"{rows[m].get(c, 0):.2f}" for m in rows) + " |")


if __name__ == "__main__":
    main()
