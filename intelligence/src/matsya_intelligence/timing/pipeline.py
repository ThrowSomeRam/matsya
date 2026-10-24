"""Payment-timing model: fit per buyer with group shrinkage, predict open invoices.

fit_timing(outcomes, invoices, buyers, as_of, run_id) -> TimingModel
Inputs are the adapter frames (io.adapters: outcomes, invoices, buyers).
TimingModel.predict(horizon_weeks) -> rows of features.invoice_timing
Seeds come from run_id and buyer_id, so a refit with the same inputs is
bit-identical.
"""

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import polars as pl

from matsya_intelligence.io.types import INVOICE_TIMING
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth.world import load_constants
from matsya_intelligence.timing.hierarchy import TYPE_DEFAULTS, group_prior
from matsya_intelligence.timing.empirical import buyer_hazard, counts, group_hazard, predict_open_empirical
from matsya_intelligence.timing.labels import timing_labels
from matsya_intelligence.timing.model import Posterior, TimingPrior, gibbs, p_dead_given_open, predict_open

EMPIRICAL_MIN_PAID = 8


@dataclass
class TimingModel:
    tenant_id: str
    as_of: date
    labels: pl.DataFrame
    priors: dict[str, TimingPrior]  # buyer_id -> prior used in the final fit
    posteriors: dict[str, Posterior]  # buyer_id -> draws
    hazards: dict[str, np.ndarray] = field(default_factory=dict)  # buyer_id -> empirical hazard (empirical mode)

    def predict_invoice(self, post: Posterior, buyer_id: str | None, age: int, horizon: int, due_week: int,
                        shift: float = 0.0) -> tuple[np.ndarray, float, float]:
        """Empirical hazard if the buyer has one (shock shift not applied there yet), else Beta-geometric."""
        h = self.hazards.get(buyer_id) if buyer_id is not None else None
        if h is None:
            return predict_open(post, age, horizon, due_week, shift)
        q = float(p_dead_given_open(age, post.p, post.pi, due_week, post.p_early).mean())
        return predict_open_empirical(h, q, age, horizon, due_week)

    def predict(self, horizon_weeks: int = 13, shock_shift: dict[str, float] | None = None) -> pl.DataFrame:
        """One row per open invoice per week ahead. shock_shift maps buyer_id -> logit shift (hook, unused)."""
        rows = []
        for r in self.labels.filter(pl.col("state") == "open").iter_rows(named=True):
            shift = (shock_shift or {}).get(r["buyer_id"], 0.0)
            paid, never, beyond = self.predict_invoice(self.posteriors[r["buyer_id"]], r["buyer_id"], r["c"],
                                                       horizon_weeks, r["d"], shift)
            for w, pw in enumerate(paid, start=1):
                rows.append((self.tenant_id, r["invoice_id"], w, float(pw), never, beyond, r["past_cutoff"]))
        return pl.DataFrame(rows, schema=INVOICE_TIMING, orient="row")


def split_buyer(labels: pl.DataFrame, buyer_id: str) -> dict[str, np.ndarray]:
    """gibbs() keyword arguments for one buyer."""
    b = labels.filter(pl.col("buyer_id") == buyer_id)
    paid, open_ = b.filter(pl.col("state") == "paid"), b.filter(pl.col("state") == "open")
    return {"paid_weeks": paid["k"].to_numpy(), "paid_due": paid["d"].to_numpy(),
            "open_ages": open_["c"].to_numpy(), "open_due": open_["d"].to_numpy()}


def fit_timing(outcomes: pl.DataFrame, invoices: pl.DataFrame, buyers: pl.DataFrame, as_of: date, run_id: str,
               hierarchical: bool = True, n_draws: int | None = None,
               type_defaults: dict[str, TimingPrior] | None = None, empirical: bool = True) -> TimingModel:
    consts = load_constants()["payment_timing"]
    cap = float(consts["prior_strength"]["value"])
    burn_in = int(consts["gibbs_sweeps"]["value"])
    n_draws = n_draws or int(consts["gibbs_draws"]["value"])
    defaults = type_defaults or TYPE_DEFAULTS
    labels = timing_labels(outcomes, invoices, as_of, consts["never_paid_cutoff_days"]["value"])
    data = {b: split_buyer(labels, b) for b in buyers["buyer_id"]}
    btype = dict(zip(buyers["buyer_id"], buyers["type"]))
    group = {b: (s, t) for b, s, t in zip(buyers["buyer_id"], buyers["sector"], buyers["type"])}

    def fit(prior: TimingPrior, b: str, stage: str) -> Posterior:
        return gibbs(prior, rng=rng_for(run_id, f"timing:{stage}:{b}"), n_draws=n_draws, burn_in=burn_in, **data[b])

    priors = {b: defaults[btype[b]] for b in data}
    posts = {b: fit(priors[b], b, "type") for b in data}
    if hierarchical:
        members: dict[tuple, list[str]] = {}
        for b in data:
            members.setdefault(group[b], []).append(b)
        for g, bs in members.items():
            if len(bs) < 2:
                continue
            gp = group_prior(np.array([posts[b].p.mean() for b in bs]),
                             np.array([posts[b].pi.mean() for b in bs]), cap,
                             np.array([posts[b].p_early.mean() for b in bs]))
            for b in bs:
                priors[b] = gp
                posts[b] = fit(gp, b, "group")
    hazards = empirical_hazards(data, posts, group, cap) if empirical else {}
    tenant = outcomes["tenant_id"][0] if outcomes.height else ""
    return TimingModel(tenant_id=tenant, as_of=as_of, labels=labels, priors=priors, posteriors=posts, hazards=hazards)


def empirical_hazards(data: dict[str, dict], posts: dict[str, Posterior], group: dict[str, tuple],
                      strength: float) -> dict[str, np.ndarray]:
    """Empirical mode (timing.empirical): hazards for buyers with >= EMPIRICAL_MIN_PAID paid bills."""
    er = {}
    for b, x in data.items():
        po = posts[b]
        alive = 1 - np.array([p_dead_given_open(c, po.p, po.pi, d, po.p_early).mean()
                              for c, d in zip(x["open_ages"], x["open_due"])])
        er[b] = counts(x["paid_weeks"], x["paid_due"], x["open_ages"], x["open_due"], alive)
    pooled: dict[tuple, list] = {}
    for b, (e, r) in er.items():
        acc = pooled.setdefault(group[b], [np.zeros_like(e), np.zeros_like(r)])
        acc[0] += e
        acc[1] += r
    return {b: buyer_hazard(e, r, group_hazard(*pooled[group[b]]), strength)
            for b, (e, r) in er.items() if data[b]["paid_weeks"].size >= EMPIRICAL_MIN_PAID}
