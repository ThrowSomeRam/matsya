"""Payment-timing model: fit per buyer with group shrinkage, predict open invoices.

fit_timing(outcomes, buyers, as_of, run_id) -> TimingModel
TimingModel.predict(horizon_weeks) -> rows of features.invoice_timing
Seeds come from run_id and buyer_id, so a refit with the same inputs is
bit-identical.
"""

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl

from matsya_intelligence.io.types import INVOICE_TIMING
from matsya_intelligence.seeding import rng_for
from matsya_intelligence.synth.world import load_constants
from matsya_intelligence.timing.hierarchy import TYPE_DEFAULTS, group_prior
from matsya_intelligence.timing.labels import timing_labels
from matsya_intelligence.timing.model import Posterior, TimingPrior, gibbs, predict_open


@dataclass
class TimingModel:
    tenant_id: str
    as_of: date
    labels: pl.DataFrame
    priors: dict[str, TimingPrior]  # buyer_id -> prior used in the final fit
    posteriors: dict[str, Posterior]  # buyer_id -> draws

    def predict(self, horizon_weeks: int = 13, shock_shift: dict[str, float] | None = None) -> pl.DataFrame:
        """One row per open invoice per week ahead. shock_shift maps buyer_id -> logit shift (hook, unused)."""
        rows = []
        for r in self.labels.filter(pl.col("state") == "open").iter_rows(named=True):
            shift = (shock_shift or {}).get(r["buyer_id"], 0.0)
            paid, never, beyond = predict_open(self.posteriors[r["buyer_id"]], r["c"], horizon_weeks, r["d"], shift)
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
               hierarchical: bool = True, n_draws: int | None = None, label_lag_days: int = 3,
               type_defaults: dict[str, TimingPrior] | None = None) -> TimingModel:
    consts = load_constants()["payment_timing"]
    cap = float(consts["prior_strength"]["value"])
    burn_in = int(consts["gibbs_sweeps"]["value"])
    n_draws = n_draws or int(consts["gibbs_draws"]["value"])
    defaults = type_defaults or TYPE_DEFAULTS
    labels = timing_labels(outcomes, invoices, as_of, label_lag_days, consts["never_paid_cutoff_days"]["value"])
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
    tenant = outcomes["tenant_id"][0] if outcomes.height else ""
    return TimingModel(tenant_id=tenant, as_of=as_of, labels=labels, priors=priors, posteriors=posts)
