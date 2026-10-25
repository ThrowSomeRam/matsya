"""Reduced-sales preset at 90 and 180 days, with and without purchase stickiness.

Generator worlds (default WorldConfig), learned inputs built at a 180-day horizon so fixed
obligations and GST dates cover the whole view. Baseline: the same world without the scenario,
on the same seed (common random numbers). n = pasts x paths.

Run: uv run --project intelligence python eval/models/scenario_views.py [n_pasts] [n_paths]
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from results import row, write  # noqa: E402

from matsya_intelligence.engine import build_learned_inputs, run_engine  # noqa: E402
from matsya_intelligence.scenarios import RateMultiplier, preset, preset_view_days, run_scenario  # noqa: E402
from matsya_intelligence.synth import WorldConfig, generate_world  # noqa: E402

NAME = "reduced_sales"


def main(n_pasts: int = 20, n_paths: int = 2000) -> None:
    days = preset_view_days(NAME)
    H = max(days)
    cfg = WorldConfig()
    variants = {"preset (stickiness 14d)": None, "stickiness 0d": 0}
    acc = {v: [] for v in variants}
    for seed in range(n_pasts):
        obs, truth = generate_world(cfg, past_seed=seed, future_seed=seed)
        cats = truth.transactions.select("txn_uid", "category")
        run_id = f"scenario-views-{seed}"
        inp = build_learned_inputs(obs, cats, cfg.as_of_date, run_id, horizon=H)
        base = run_engine(inp, run_id, n_paths, H)
        for v, sticky in variants.items():
            ts = preset(NAME, cfg.as_of_date)
            if sticky is not None:
                ts = [RateMultiplier(target="orders", factor=ts[0].factor, purchase_stickiness_days=sticky)]
            acc[v].append(run_scenario(inp, run_id, ts, NAME, n_paths, H, base=base).at_days(days))

    cols = ["p_short_base", "p_short", "expected_shortfall_uncond_paise_base", "expected_shortfall_uncond_paise",
            "expected_shortfall_cond_paise_base", "expected_shortfall_cond_paise"]
    lines = [f"# Reduced sales at {', '.join(map(str, days))} days\n",
             f"Default generator world, {n_pasts} pasts x {n_paths:,} paths, learned inputs at a {H}-day horizon. "
             "Orders x 0.7. Baseline: same world and seed, no scenario. Means over pasts; ES in Rs lakh "
             "(unconditional = p_short x mean depth; conditional = mean depth over short paths, mean over "
             "pasts with any short path).\n",
             "| variant | day | p_short base | p_short | d p_short (sd over pasts) | ES uncond base | ES uncond | "
             "ES cond base | ES cond |", "|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for v, frames in acc.items():
        for i, d in enumerate(days):
            x = {c: np.array([f[c][i] for f in frames], dtype=float) for c in cols}
            dp = x["p_short"] - x["p_short_base"]
            lk = lambda a: a.mean() / 1e7  # noqa: E731  paise -> Rs lakh
            cond = lambda a: a[a > 0].mean() / 1e7 if (a > 0).any() else 0.0  # noqa: E731
            lines.append(f"| {v} | {d} | {x['p_short_base'].mean():.3f} | {x['p_short'].mean():.3f} | "
                         f"{dp.mean():+.3f} ({dp.std(ddof=1) if len(dp) > 1 else 0:.3f}) | "
                         f"{lk(x['expected_shortfall_uncond_paise_base']):.2f} | "
                         f"{lk(x['expected_shortfall_uncond_paise']):.2f} | "
                         f"{cond(x['expected_shortfall_cond_paise_base']):.2f} | "
                         f"{cond(x['expected_shortfall_cond_paise']):.2f} |")
            model = "scenario_reduced_sales" + ("" if variants[v] is None else "_sticky0")
            n = n_pasts * n_paths
            rows += [row("scenario_views", "generator_default", "scenario engine", model, "d_p_short", dp.mean(), n,
                         "same world, no scenario (CRN)", horizon=d),
                     row("scenario_views", "generator_default", "scenario engine", model, "p_short",
                         x["p_short"].mean(), n, f"baseline {x['p_short_base'].mean():.3f}", horizon=d),
                     row("scenario_views", "generator_default", "scenario engine", model, "es_uncond_paise",
                         x["expected_shortfall_uncond_paise"].mean(), n,
                         f"baseline {x['expected_shortfall_uncond_paise_base'].mean():.0f}", horizon=d)]
    md = "\n".join(lines) + "\n"
    print(md)
    write(rows, md, "scenario_views")


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:]))
