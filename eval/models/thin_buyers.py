"""Does group shrinkage help buyers with few paid bills? (payment-timing model, generator worlds)

    uv run --project intelligence python eval/models/thin_buyers.py [n_worlds] [workers]

Worlds: the default (borderline) config with 40 buyers instead of 10, so the order mix leaves many
buyers with only a few invoices; past seeds 0..n-1. For every buyer with 1 to 3 paid bills in its
as-of labels, the payment-timing model is fitted twice on the same labels and seed: with group
shrinkage (the default: type prior, then a (sector, type) group prior from the other buyers) and
without (type prior only). Error = |posterior mean - the generator's true value| for p (weekly
chance from the due week), p_early (before it), and expected weeks to pay once due (1/p). Paired
differences, bootstrap over buyers (5,000 resamples). Heavy buyers (>= 20 paid bills) are scored
the same way as a check that shrinkage leaves them alone.
"""

import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import md_table, row, write  # noqa: E402

N_BUYERS = 40
THIN = (1, 3)
HEAVY = 20


def one_world(seed: int) -> list[dict]:
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    from matsya_intelligence.io.adapters import track_b_inputs
    from matsya_intelligence.synth import WorldConfig, generate_world
    from matsya_intelligence.timing import fit_timing

    cfg = replace(WorldConfig(), n_buyers=N_BUYERS)
    tables, truth = generate_world(cfg, past_seed=seed, future_seed=0)
    obs = track_b_inputs(tables, cfg.as_of)
    args = (obs["invoice_outcomes"], obs["invoices"], obs["buyers"], obs["complete_through"])
    run_id = f"thin-buyers:{seed}"
    fits = {h: fit_timing(*args, run_id=run_id, hierarchical=h, empirical=False) for h in (True, False)}
    true = {b["buyer_id"]: b for b in truth.buyer_records}
    paid = fits[True].labels.filter(pl.col("state") == "paid").group_by("buyer_id").len()
    n_paid = dict(zip(paid["buyer_id"], paid["len"]))
    out = []
    for b, post in fits[True].posteriors.items():
        n = n_paid.get(b, 0)
        if not (THIN[0] <= n <= THIN[1] or n >= HEAVY):
            continue
        t = true[b]
        rec = {"seed": seed, "buyer_id": b, "n_paid": n, "group": "thin" if n <= THIN[1] else "heavy"}
        for name, h in (("shrunk", True), ("type_only", False)):
            p = fits[h].posteriors[b]
            rec[f"{name}_err_p"] = abs(float(p.p.mean()) - t["p"])
            rec[f"{name}_err_p_early"] = abs(float(p.p_early.mean()) - t["p_early"])
            rec[f"{name}_err_weeks"] = abs(float((1 / p.p).mean()) - 1 / t["p"])
        out.append(rec)
    return out


def main(n_worlds: int = 30, workers: int = 4) -> None:
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
        recs = [r for w in ex.map(one_world, range(n_worlds)) for r in w]
    df = pl.DataFrame(recs)
    rng = np.random.default_rng(20261025)
    table, rows = [], []
    for group in ("thin", "heavy"):
        g = df.filter(pl.col("group") == group)
        n = g.height
        idx = rng.integers(0, n, (5000, n))
        for metric, label in (("p", "weekly chance once due, p"), ("p_early", "weekly chance before due, p_early"),
                              ("weeks", "expected weeks to pay once due, 1/p")):
            a, b = g[f"shrunk_err_{metric}"].to_numpy(), g[f"type_only_err_{metric}"].to_numpy()
            d = a - b
            bm = d[idx].mean(axis=1)
            lo, hi = np.percentile(bm, [2.5, 97.5])
            verdict = "shrinkage better" if hi < 0 else ("shrinkage worse" if lo > 0 else "no clear difference")
            table.append({"buyers": f"{group} (n = {n})", "quantity": label, "MAE with shrinkage": f"{a.mean():.4f}",
                          "MAE type prior only": f"{b.mean():.4f}", "difference (95% CI)": f"{d.mean():+.4f} ({lo:+.4f} to {hi:+.4f})",
                          "better in": f"{(d < 0).mean():.0%}", "verdict": verdict})
            for model, v in (("group_shrinkage", a), ("type_prior_only", b)):
                rows.append(row("thin_buyers", "generator_40_buyers", "payment-timing model", model, f"mae_{metric}",
                                v.mean(), n, "type_prior_only", horizon=group,
                                note=f"{THIN[0]}-{THIN[1]} paid bills" if group == "thin" else f">= {HEAVY} paid bills"))
    md = (f"# Group shrinkage for thin buyers (payment-timing model)\n\n{n_worlds} generator worlds (borderline config "
          f"with {N_BUYERS} buyers), past seeds 0-{n_worlds - 1}. Thin = {THIN[0]}-{THIN[1]} paid bills at as_of, heavy "
          f">= {HEAVY}. Error = |posterior mean - true value|; difference = with shrinkage minus type prior only "
          "(negative favours shrinkage), paired bootstrap over buyers.\n\n" + md_table(pl.DataFrame(table)) + "\n")
    write(rows, md, "thin_buyers")
    print(md)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
