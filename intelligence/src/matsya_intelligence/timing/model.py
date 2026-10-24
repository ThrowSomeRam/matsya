"""Beta-geometric payment timing with pre-due and due-week phases and a cure fraction, for one buyer.

Weeks since issue, T >= 1. Week d is the week holding the invoice's due date,
d = terms_days // 7 + 1. An alive invoice is paid in week k with chance
p_early if k < d, p_due if k = d, p if k > d. It is dead (never paid) with chance pi.

    p_early ~ Beta(alpha_e, beta_e),  p_due ~ Beta(alpha_d, beta_d),  p ~ Beta(alpha, beta),
    pi ~ Beta(a, b)

Each week is one Bernoulli trial on exactly one of the three chances, so the likelihood
factorises and each Beta is updated on its own counts. Each unpaid week adds one to the
beta count of its phase; a payment adds one to the alpha of its phase. For an invoice paid
in week k:
    early failures min(k - 1, d - 1), due-week failures [k > d], late failures max(0, k - d - 1)
For one still open after c full weeks:
    early failures min(c, d - 1),     due-week failures [c >= d], late failures max(0, c - d)
    survival S(c) = (1 - p_early)^min(c, d-1) (1 - p_due)^[c >= d] (1 - p)^max(0, c-d)

due_phase=False is the two-phase model: the due week shares p with later weeks (its
counts are added to p's), p_due is not drawn and Posterior.p_due is None.

With pi = 0 this is conjugate (conjugate_update). With pi > 0, Gibbs over a
dead flag D_j per open invoice:
    (a) P(D_j = 1 | c_j, ...) = pi / (pi + (1 - pi) S(c_j))
    (b) pi | D ~ Beta(a + n_dead, b + n_alive)
    (c) p_early, p_due, p | D: the conjugate update using only paid and alive open invoices
d = 1 (due in the first week): week 1 uses p_due, later weeks p (two-phase: p throughout).
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
    alpha_d: float | None = None  # p_due ~ Beta(alpha_d, beta_d), the due week; None = p's prior
    beta_d: float | None = None

    @property
    def due_ab(self) -> tuple[float, float]:
        return (self.alpha, self.beta) if self.alpha_d is None else (self.alpha_d, self.beta_d)

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
    p_due: np.ndarray | None = None  # None: two-phase model, the due week uses p

    @property
    def p_due_or_p(self) -> np.ndarray:
        return self.p if self.p_due is None else self.p_due

    def interval(self, which: str, level: float = 0.9) -> tuple[float, float]:
        x = getattr(self, which)
        lo = (1 - level) / 2
        return float(np.quantile(x, lo)), float(np.quantile(x, 1 - lo))


def _due(d, n: int) -> np.ndarray:
    return np.ones(n) if d is None else np.asarray(d, dtype=float)


def _open_failures(c: np.ndarray, d: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per open invoice: (early, due-week, late) failures after c full weeks."""
    return np.minimum(c, d - 1), (c >= d).astype(float), np.maximum(0, c - d)


def phase_counts3(paid_weeks, paid_due, open_ages, open_due):
    """(early s, early f, due-week s, due-week f, late s, late f) for paid + given open invoices."""
    k = np.asarray(paid_weeks, dtype=float)
    dk = _due(paid_due, k.size)
    c = np.asarray(open_ages, dtype=float)
    dc = _due(open_due, c.size)
    oe, od, ol = _open_failures(c, dc)
    return (
        float((k < dk).sum()),
        float(np.minimum(k - 1, dk - 1).sum() + oe.sum()),
        float((k == dk).sum()),
        float((k > dk).sum() + od.sum()),
        float((k > dk).sum()),
        float(np.maximum(0, k - dk - 1).sum() + ol.sum()),
    )


def phase_counts(paid_weeks, paid_due, open_ages, open_due):
    """(early successes, early failures, late successes, late failures), due week counted as late."""
    se, fe, sd, fd, sl, fl = phase_counts3(paid_weeks, paid_due, open_ages, open_due)
    return se, fe, sd + sl, fd + fl


def conjugate_update(prior: TimingPrior, paid_weeks, open_ages, paid_due=None, open_due=None):
    """Two-phase (alpha_e', beta_e', alpha', beta'), ignoring the cure fraction."""
    se, fe, sl, fl = phase_counts(paid_weeks, paid_due, open_ages, open_due)
    return prior.alpha_e + se, prior.beta_e + fe, prior.alpha + sl, prior.beta + fl


def conjugate_update3(prior: TimingPrior, paid_weeks, open_ages, paid_due=None, open_due=None):
    """Three-phase (alpha_e', beta_e', alpha_d', beta_d', alpha', beta'), ignoring the cure fraction."""
    se, fe, sd, fd, sl, fl = phase_counts3(paid_weeks, paid_due, open_ages, open_due)
    ad, bd = prior.due_ab
    return prior.alpha_e + se, prior.beta_e + fe, ad + sd, bd + fd, prior.alpha + sl, prior.beta + fl


def survival(c, d, p_early, p, p_due=None):
    c = np.asarray(c, dtype=float)
    d = np.asarray(d, dtype=float)
    oe, od, ol = _open_failures(c, d)
    return (1 - p_early) ** oe * (1 - (p if p_due is None else p_due)) ** od * (1 - p) ** ol


def p_dead_given_open(c, p, pi, d=1, p_early=None, p_due=None):
    """(a): chance an invoice still open after c full weeks will never be paid."""
    s = survival(c, d, p if p_early is None else p_early, p, p_due)
    return pi / (pi + (1 - pi) * s)


def gibbs(prior: TimingPrior, paid_weeks, open_ages, rng: np.random.Generator, paid_due=None, open_due=None,
          n_draws: int = 200, burn_in: int = 20, fix_pi: float | None = None,
          init: tuple[float, float, float] | None = None, due_phase: bool = False) -> Posterior:
    """Posterior draws of (p, pi, p_early[, p_due]). fix_pi pins pi; init = (p_early, p, pi) start
    values (p_due starts at p). due_phase=False is the two-phase model."""
    k = np.asarray(paid_weeks, dtype=float)
    c = np.asarray(open_ages, dtype=float)
    dk, dc = _due(paid_due, k.size), _due(open_due, c.size)
    se, fe_paid, sd, fd_paid, sl, fl_paid = phase_counts3(k, dk, [], [])
    fe_open, fd_open, fl_open = _open_failures(c, dc)
    if not due_phase:  # fold the due week into p
        sl, fl_paid, fl_open = sl + sd, fl_paid + fd_paid, fl_open + fd_open
        fd_open = np.zeros_like(fl_open)
    ad, bd = prior.due_ab
    n_paid = k.size
    if init is None:
        ae, be, al, bl = conjugate_update(prior, k, c, dk, dc)
        pe, p, pi = rng.beta(ae, be), rng.beta(al, bl), prior.pi_mean
    else:
        pe, p, pi = init
    pd = p
    if fix_pi is not None:
        pi = fix_pi
    out = np.empty((n_draws, 4))
    for t in range(burn_in + n_draws):
        s = (1 - pe) ** fe_open * (1 - pd) ** fd_open * (1 - p) ** fl_open
        dead = rng.random(c.size) < pi / (pi + (1 - pi) * s)
        n_dead = int(dead.sum())
        if fix_pi is None:
            pi = rng.beta(prior.a + n_dead, prior.b + n_paid + c.size - n_dead)
        alive = ~dead
        pe = rng.beta(prior.alpha_e + se, prior.beta_e + fe_paid + float(fe_open[alive].sum()))
        if due_phase:
            pd = rng.beta(ad + sd, bd + fd_paid + float(fd_open[alive].sum()))
        p = rng.beta(prior.alpha + sl, prior.beta + fl_paid + float(fl_open[alive].sum()))
        if t >= burn_in:
            out[t - burn_in] = (p, pi, pe, pd)
    return Posterior(p=out[:, 0], pi=out[:, 1], p_early=out[:, 2], p_due=out[:, 3] if due_phase else None)


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
    Averaged over posterior draws. The shock shift applies to every phase."""
    p = shift_logit(post.p, shock_shift)
    pe = shift_logit(post.p_early, shock_shift)
    pd = shift_logit(post.p_due_or_p, shock_shift)
    q = p_dead_given_open(age, p, post.pi, due_week, pe, pd)
    w = (age + np.arange(1, horizon + 1))[None, :]  # weeks since issue
    h = np.where(w < due_week, pe[:, None], np.where(w == due_week, pd[:, None], p[:, None]))  # (draws, horizon)
    survive_before = np.cumprod(np.hstack([np.ones((h.shape[0], 1)), 1 - h[:, :-1]]), axis=1)
    paid = ((1 - q)[:, None] * survive_before * h).mean(axis=0)
    beyond = float(((1 - q) * np.prod(1 - h, axis=1)).mean())
    return paid, float(q.mean()), beyond
