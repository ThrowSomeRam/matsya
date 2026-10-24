"""Beta-geometric payment timing with a cure fraction, for one buyer.

Weeks since issue, T >= 1. An alive invoice is paid in each week with chance
p; an invoice is dead (never paid) with chance pi.

    p ~ Beta(alpha, beta),   pi ~ Beta(a, b)
    paid in week k:          (1 - pi) (1 - p)^(k-1) p
    open after c full weeks: pi + (1 - pi) (1 - p)^c

With pi = 0 the update is conjugate (conjugate_update). With pi > 0 the open
invoices are ambiguous, so a latent dead flag D_j per open invoice is sampled
(gibbs):
    (a) P(D_j = 1 | c_j, p, pi) = pi / (pi + (1 - pi) (1 - p)^c_j)
    (b) pi | D ~ Beta(a + n_dead, b + n_alive),  alive = paid + open with D = 0
    (c) p | D  ~ Beta(alpha + n_paid, beta + sum(k_i - 1) + sum(c_j : D_j = 0))
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TimingPrior:
    alpha: float  # p ~ Beta(alpha, beta)
    beta: float
    a: float  # pi ~ Beta(a, b)
    b: float

    @property
    def p_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def pi_mean(self) -> float:
        return self.a / (self.a + self.b)


@dataclass(frozen=True)
class Posterior:
    p: np.ndarray  # draws
    pi: np.ndarray

    def interval(self, which: str, level: float = 0.9) -> tuple[float, float]:
        x = getattr(self, which)
        lo = (1 - level) / 2
        return float(np.quantile(x, lo)), float(np.quantile(x, 1 - lo))


def conjugate_update(prior: TimingPrior, paid_weeks: np.ndarray, open_ages: np.ndarray) -> tuple[float, float]:
    """(alpha', beta') for p, ignoring the cure fraction."""
    k = np.asarray(paid_weeks, dtype=float)
    c = np.asarray(open_ages, dtype=float)
    return prior.alpha + k.size, prior.beta + float((k - 1).sum() + c.sum())


def p_dead_given_open(c, p, pi):
    """(a): chance an invoice still open after c full weeks will never be paid."""
    c = np.asarray(c, dtype=float)
    survive = (1 - p) ** c
    return pi / (pi + (1 - pi) * survive)


def gibbs(prior: TimingPrior, paid_weeks: np.ndarray, open_ages: np.ndarray, rng: np.random.Generator,
          n_draws: int = 200, burn_in: int = 20, fix_pi: float | None = None) -> Posterior:
    """Posterior draws of (p, pi). fix_pi pins pi (e.g. 0) instead of sampling it."""
    k = np.asarray(paid_weeks, dtype=float)
    c = np.asarray(open_ages, dtype=float)
    n_paid, paid_fail = k.size, float((k - 1).sum())
    pi = prior.pi_mean if fix_pi is None else fix_pi
    p = rng.beta(*conjugate_update(prior, k, c))
    ps, pis = np.empty(n_draws), np.empty(n_draws)
    for t in range(burn_in + n_draws):
        dead = rng.random(c.size) < p_dead_given_open(c, p, pi)
        n_dead = int(dead.sum())
        if fix_pi is None:
            pi = rng.beta(prior.a + n_dead, prior.b + n_paid + c.size - n_dead)
        p = rng.beta(prior.alpha + n_paid, prior.beta + paid_fail + float(c[~dead].sum()))
        if t >= burn_in:
            ps[t - burn_in], pis[t - burn_in] = p, pi
    return Posterior(p=ps, pi=pis)


def shift_logit(p: np.ndarray, shift: float) -> np.ndarray:
    """Hook for the scenario engine's sector shock: logit p + shift. Unused yet (shift 0)."""
    if shift == 0.0:
        return p
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return 1 / (1 + np.exp(-(np.log(p / (1 - p)) + shift)))


def predict_open(post: Posterior, age: int, horizon: int, shock_shift: float = 0.0) -> tuple[np.ndarray, float, float]:
    """For an invoice open after `age` full weeks: P(paid in week w from now), w = 1..horizon,
    P(never), P(alive but unpaid beyond horizon). Averaged over posterior draws."""
    p = shift_logit(post.p, shock_shift)
    q = p_dead_given_open(age, p, post.pi)
    w = np.arange(1, horizon + 1)
    paid = ((1 - q)[:, None] * (1 - p)[:, None] ** (w - 1) * p[:, None]).mean(axis=0)
    beyond = float(((1 - q) * (1 - p) ** horizon).mean())
    return paid, float(q.mean()), beyond
