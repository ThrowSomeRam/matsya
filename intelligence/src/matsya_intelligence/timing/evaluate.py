"""Recovery and benchmark harness for the payment-timing model on generator worlds.

recovery(): fit each buyer under the TRUE type hyperprior (a well-specified
Bayesian check), report how often 90% intervals contain the planted p_early,
p and pi, and the mean absolute error of the posterior means.

rhat_check(): 4 chains per buyer from over-dispersed starts (drawn from the
prior), split R-hat per parameter.

benchmark(): fit the real pipeline (type defaults + group shrinkage) on the
observed tables at as_of, predict every open invoice, score against the true
final paid date. Baseline: the due-date assumption (paid in the week of its
due date; overdue invoices in week 1), smoothed so its log score is finite:
DUE_DATE_MASS on that outcome, the rest spread evenly over all H + 1 outcomes.
Outcomes: paid in week 1..H from as_of, or "not within H" (never or later).
The generator pays slowly before the due week (pre_due_phase), so the
due-date assumption is a fair, not a straw, baseline.
"""

from dataclasses import replace
from datetime import date

import numpy as np
import polars as pl

from matsya_intelligence.seeding import rng_for
from matsya_intelligence.io.adapters import track_b_inputs
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.world import synth_tenant_id
from matsya_intelligence.timing.labels import timing_labels
from matsya_intelligence.timing.model import TimingPrior, gibbs, split_rhat
from matsya_intelligence.timing.pipeline import fit_timing, split_buyer

PARAMS = ("p_early", "p", "pi")


def true_hyperpriors(cfg: WorldConfig) -> dict[str, TimingPrior]:
    return {t: TimingPrior(*tp.p_ab, *tp.pi_ab, *tp.p_early_ab) for t, tp in cfg.type_priors.items()}

DUE_DATE_MASS = 0.9
CAL_WEEKS = (4, 8, 12)


def _buyer_data(cfg: WorldConfig, seeds):
    for s in seeds:
        tables, truth = generate_world(cfg, past_seed=s, future_seed=0)
        obs = track_b_inputs(tables, cfg.as_of)
        lab = timing_labels(obs["invoice_outcomes"], obs["invoices"], cfg.as_of_date)
        for b in truth.buyers.iter_rows(named=True):
            yield s, b, split_buyer(lab, b["buyer_id"])


def recovery(cfg: WorldConfig, seeds, n_draws: int = 1000) -> dict:
    hyper = true_hyperpriors(cfg)
    hit = {n: [] for n in PARAMS}
    err = {n: [] for n in PARAMS}
    for s, b, data in _buyer_data(cfg, seeds):
        post = gibbs(hyper[b["type"]], rng=rng_for(f"recovery-{s}", b["buyer_id"]), n_draws=n_draws, **data)
        for name in PARAMS:
            lo, hi = post.interval(name, 0.9)
            hit[name].append(lo <= b[name] <= hi)
            err[name].append(abs(getattr(post, name).mean() - b[name]))
    out = {"n_buyers": len(hit["p"])}
    for name in PARAMS:
        out[f"coverage_{name}"] = float(np.mean(hit[name]))
        out[f"mae_{name}"] = float(np.mean(err[name]))
    return out


def rhat_check(cfg: WorldConfig, seeds, n_chains: int = 4, n_draws: int = 400, burn_in: int = 50) -> dict:
    hyper = true_hyperpriors(cfg)
    rhats = {n: [] for n in PARAMS}
    for s, b, data in _buyer_data(cfg, seeds):
        prior = hyper[b["type"]]
        rng = rng_for(f"rhat-{s}", b["buyer_id"])
        chains = []
        for _ in range(n_chains):
            init = (rng.beta(prior.alpha_e, prior.beta_e), rng.beta(prior.alpha, prior.beta), rng.beta(prior.a, prior.b))
            chains.append(gibbs(prior, rng=rng, n_draws=n_draws, burn_in=burn_in, init=init, **data))
        for name in PARAMS:
            rhats[name].append(split_rhat(np.stack([getattr(ch, name) for ch in chains])))
    out = {"n_buyers": len(rhats["p"]), "n_chains": n_chains, "n_draws": n_draws, "burn_in": burn_in}
    for name in PARAMS:
        r = np.array(rhats[name])
        out[f"max_rhat_{name}"] = float(r.max())
        out[f"share_below_1.01_{name}"] = float((r < 1.01).mean())
    return out


def _outcome_week(paid: date | None, as_of: date, horizon: int) -> int:
    """1..horizon, or horizon + 1 for not within the horizon."""
    if paid is None:
        return horizon + 1
    w = max(1, (paid - as_of).days // 7 + 1)
    return w if w <= horizon else horizon + 1


def _calibration(pred: np.ndarray, hit: np.ndarray, bins: int = 10) -> dict:
    idx = np.minimum((pred * bins).astype(int), bins - 1)
    ece, table = 0.0, []
    for b in range(bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(pred[m].mean() - hit[m].mean())
            table.append((round(float(pred[m].mean()), 3), round(float(hit[m].mean()), 3), int(m.sum())))
    return {"brier": float(np.mean((pred - hit) ** 2)), "ece": float(ece), "reliability": table}


def benchmark(seeds, cfg: WorldConfig | None = None, horizon: int = 13) -> dict:
    cfg = cfg or WorldConfig()
    model_dist, due_dist, outcomes = [], [], []
    for s in seeds:
        tables, truth = generate_world(replace(cfg, tenant_id=synth_tenant_id(f"t{s}")), past_seed=s, future_seed=0)
        obs = track_b_inputs(tables, cfg.as_of)
        model = fit_timing(obs["invoice_outcomes"], obs["invoices"], obs["buyers"], cfg.as_of_date, run_id=f"bench-{s}")
        pred = model.predict(horizon)
        inv = truth.invoices.select("invoice_id", "due_date", "paid_date")
        for (iid,), g in pred.sort("week").group_by(["invoice_id"], maintain_order=True):
            row = inv.filter(pl.col("invoice_id") == iid).row(0, named=True)
            dist = np.append(g["p_paid"].to_numpy(), g["p_never"][0] + g["p_beyond"][0])
            model_dist.append(dist / dist.sum())
            due_w = _outcome_week(row["due_date"], cfg.as_of_date, horizon)
            d = np.full(horizon + 1, (1 - DUE_DATE_MASS) / (horizon + 1))
            d[due_w - 1] += DUE_DATE_MASS
            due_dist.append(d)
            outcomes.append(_outcome_week(row["paid_date"], cfg.as_of_date, horizon))
    model_dist, due_dist, y = np.array(model_dist), np.array(due_dist), np.array(outcomes)
    rows = np.arange(len(y))
    result = {"n_invoices": len(y), "n_worlds": len(list(seeds))}
    for name, dist in (("model", model_dist), ("due_date", due_dist)):
        r = {"log_score": float(np.mean(np.log(dist[rows, y - 1])))}
        for h in CAL_WEEKS:
            r[f"within_{h}w"] = _calibration(dist[:, :h].sum(axis=1), (y <= h).astype(float))
        result[name] = r
    return result


ROBUSTNESS_KNOBS = {
    "baseline (shocks off, no part-payments)": {},
    "single-phase world (pre_due_phase off)": {"pre_due_phase": False},
    "sector shocks on (sigma 0.3)": {"shock_sigma": 0.3},
    "part-payments 10% (final date used)": {"part_payment_prob": 0.1},
    "lognormal lateness": {"lateness": "lognormal"},
    "month-end clustering": {"month_end_clustering": True},
    "behaviour change (buyer 0 of 10 per world, p x0.5 from 4 months before as_of)": "behaviour_change",
}


def robustness(seeds) -> dict:
    from datetime import timedelta

    from matsya_intelligence.synth import BehaviourChange

    base = WorldConfig(shock_sigma=0.0, part_payment_prob=0.0)
    out = {}
    for name, knob in ROBUSTNESS_KNOBS.items():
        if knob == "behaviour_change":
            cfg = replace(base, behaviour_change=BehaviourChange(
                buyer_index=0, at=base.as_of_date - timedelta(days=120), p_multiplier=0.5))
        else:
            cfg = replace(base, **knob)
        out[name] = recovery(cfg, seeds, n_draws=400)
    return out
