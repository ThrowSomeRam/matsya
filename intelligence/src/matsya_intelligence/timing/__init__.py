"""Payment-timing model: Beta-geometric with a cure fraction, per buyer, shrunk to sector+type;
buyers with >= 8 paid bills use an empirical hazard by week relative to due (timing.empirical)."""

from matsya_intelligence.timing.model import Posterior, TimingPrior, conjugate_update, gibbs, p_dead_given_open, predict_open
from matsya_intelligence.timing.pipeline import TimingModel, fit_timing

__all__ = ["Posterior", "TimingModel", "TimingPrior", "conjugate_update", "fit_timing", "gibbs",
           "p_dead_given_open", "predict_open"]
