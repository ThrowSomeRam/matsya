"""Beta-geometric payment timing with a pre-due phase and a cure fraction, for one buyer.

Weeks since issue, T >= 1. Week d is the week holding the invoice's due date,
d = terms_days // 7 + 1. An alive invoice is paid in week k with chance
p_early if k < d, p if k >= d. It is dead (never paid) with chance pi.

    p_early ~ Beta(alpha_e, beta_e),  p ~ Beta(alpha, beta),  pi ~ Beta(a, b)

Each unpaid week adds one to the beta count of the phase it fell in; a payment
adds one to the alpha of its phase. For an invoice paid in week k:
    early failures min(k - 1, d - 1), late failures max(0, k - d)
For one still open after c full weeks:
    early failures min(c, d - 1),     late failures max(0, c - d + 1)
    survival S(c) = (1 - p_early)^min(c, d-1) (1 - p)^max(0, c-d+1)

With pi = 0 this is conjugate (conjugate_update). With pi > 0, Gibbs over a
dead flag D_j per open invoice:
    (a) P(D_j = 1 | c_j, ...) = pi / (pi + (1 - pi) S(c_j))
    (b) pi | D ~ Beta(a + n_dead, b + n_alive)
    (c) p_early, p | D: the conjugate update using only paid and alive open invoices
d = 1 (due in the first week) makes the model single-phase in p.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TimingPrior:
    alpha: float  # p ~ Beta(alpha, beta), weeks on or after the due week
    beta: float
    a: float  # pi ~ Beta(a, b)
    b: float
    alpha_e: float = 1.0  # p_early ~ Beta(alpha_e, beta_e), weeks before the due week
    beta_e: float = 1.0

    @property
    def p_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def p_early_mean(self) -> float:
        return self.alpha_e / (self.alpha_e + self.beta_e)

    @property
    def pi_mean(self) -> float:
        return self.a / (self.a + self.b)


@dataclass(frozen=True)
class Posterior:
    p: np.ndarray  # draws
    pi: np.ndarray
    p_early: np.ndarray

    def interval(self, which: str, level: float = 0.9) -> tuple[float, float]:
        x = getattr(self, which)
        lo = (1 - level) / 2
        return float(np.quantile(x, lo)), float(np.quantile(x, 1 - lo))


def _due(d, n: int) -> np.ndarray:
    return np.ones(n) if d is None else np.asarray(d, dtype=float)


def phase_counts(paid_weeks, paid_due, open_ages, open_due):
    """(early successes, early failures, late successes, late failures) for paid + given open invoices."""
    k = np.asarray(paid_weeks, dtype=float)
    dk = _due(paid_due, k.size)
    c = np.asarray(open_ages, dtype=float)
    dc = _due(open_due, c.size)
    early = k < dk
    return (
        float(early.sum()),
        float(np.minimum(k - 1, dk - 1).sum() + np.minimum(c, dc - 1).sum()),
        float((~early).sum()),
        float(np.maximum(0, k - dk).sum() + np.maximum(0, c - dc + 1).sum()),
    )


def conjugate_update(prior: TimingPrior, paid_weeks, open_ages, paid_due=None, open_due=None):
    """(alpha_e', beta_e', alpha', beta'), ignoring the cure fraction."""
    se, fe, sl, fl = phase_counts(paid_weeks, paid_due, open_ages, open_due)
    return prior.alpha_e + se, prior.beta_e + fe, prior.alpha + sl, prior.beta + fl


def survival(c, d, p_early, p):
    c = np.asarray(c, dtype=float)
    d = np.asarray(d, dtype=float)
    return (1 - p_early) ** np.minimum(c, d - 1) * (1 - p) ** np.maximum(0, c - d + 1)


def p_dead_given_open(c, p, pi, d=1, p_early=None):
    """(a): chance an invoice still open after c full weeks will never be paid."""
    s = survival(c, d, p if p_early is None else p_early, p)
    return pi / (pi + (1 - pi) * s)


def gibbs(prior: TimingPrior, paid_weeks, open_ages, rng: np.random.Generator, paid_due=None, open_due=None,
          n_draws: int = 200, burn_in: int = 20, fix_pi: float | None = None,
          init: tuple[float, float, float] | None = None) -> Posterior:
    """Posterior draws of (p, pi, p_early). fix_pi pins pi; init = (p_early, p, pi) start values."""
    k = np.asarray(paid_weeks, dtype=float)
    c = np.asarray(open_ages, dtype=float)
    dk, dc = _due(paid_due, k.size), _due(open_due, c.size)
    se, fe_paid, sl, fl_paid = phase_counts(k, dk, [], [])
    fe_open, fl_open = np.minimum(c, dc - 1), np.maximum(0, c - dc + 1)
    n_paid = k.size
    if init is None:
        ae, be, al, bl = conjugate_update(prior, k, c, dk, dc)
        pe, p, pi = rng.beta(ae, be), rng.beta(al, bl), prior.pi_mean
    else:
        pe, p, pi = init
    if fix_pi is not None:
        pi = fix_pi
    out = np.empty((n_draws, 3))
    for t in range(burn_in + n_draws):
        s = (1 - pe) ** fe_open * (1 - p) ** fl_open
        dead = rng.random(c.size) < pi / (pi + (1 - pi) * s)
        n_dead = int(dead.sum())
        if fix_pi is None:
            pi = rng.beta(prior.a + n_dead, prior.b + n_paid + c.size - n_dead)
        alive = ~dead
        pe = rng.beta(prior.alpha_e + se, prior.beta_e + fe_paid + float(fe_open[alive].sum()))
        p = rng.beta(prior.alpha + sl, prior.beta + fl_paid + float(fl_open[alive].sum()))
        if t >= burn_in:
            out[t - burn_in] = (p, pi, pe)
    return Posterior(p=out[:, 0], pi=out[:, 1], p_early=out[:, 2])


def split_rhat(chains: np.ndarray) -> float:
    """Split R-hat (Gelman et al.) for an (n_chains, n_draws) array."""
    n = chains.shape[1] // 2
    x = np.concatenate([chains[:, :n], chains[:, n:2 * n]])
    w = x.var(axis=1, ddof=1).mean()
    b = n * x.mean(axis=1).var(ddof=1)
    if w == 0:
        return 1.0
    return float(np.sqrt(((n - 1) / n * w + b / n) / w))


def shift_logit(p: np.ndarray, shift: float) -> np.ndarray:
    """Hook for the scenario engine's sector shock: logit p + shift. Unused yet (shift 0)."""
    if shift == 0.0:
        return p
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return 1 / (1 + np.exp(-(np.log(p / (1 - p)) + shift)))


def predict_open(post: Posterior, age: int, horizon: int, due_week: int = 1,
                 shock_shift: float = 0.0) -> tuple[np.ndarray, float, float]:
    """For an invoice open after `age` full weeks with due week d: P(paid in week w from now)
    (week age + w since issue), w = 1..horizon; P(never); P(alive, unpaid beyond horizon).
    Averaged over posterior draws. The shock shift applies to both phases."""
    p = shift_logit(post.p, shock_shift)
    pe = shift_logit(post.p_early, shock_shift)
    q = p_dead_given_open(age, p, post.pi, due_week, pe)
    weeks_since_issue = age + np.arange(1, horizon + 1)
    h = np.where(weeks_since_issue[None, :] < due_week, pe[:, None], p[:, None])  # (draws, horizon)
    survive_before = np.cumprod(np.hstack([np.ones((h.shape[0], 1)), 1 - h[:, :-1]]), axis=1)
    paid = ((1 - q)[:, None] * survive_before * h).mean(axis=0)
    beyond = float(((1 - q) * np.prod(1 - h, axis=1)).mean())
    return paid, float(q.mean()), beyond
