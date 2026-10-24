import time
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from matsya_contracts.validate import validate
from matsya_intelligence.io.adapters import track_b_inputs
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.timing import TimingPrior, conjugate_update, fit_timing, gibbs, p_dead_given_open, predict_open
from matsya_intelligence.timing.evaluate import benchmark, recovery, rhat_check
from matsya_intelligence.timing.hierarchy import group_prior
from matsya_intelligence.timing.labels import timing_labels

RNG = np.random.default_rng


def test_conjugate_update_exact():
    # Due in week 1: everything is in the late phase, p_early's prior is untouched.
    ae, be, a, b = conjugate_update(TimingPrior(2, 6, 1, 1), np.array([2, 5, 3]), np.array([4, 6]))
    assert (a, b) == (5, 23)
    assert (ae, be) == (1, 1)


def test_two_phase_conjugate_counts():
    # Due week 3. Paid in week 2 (early success, 1 early failure); paid in week 5
    # (2 early failures, 2 late failures, late success); open at 4 (2 early, 2 late failures).
    ae, be, a, b = conjugate_update(TimingPrior(2, 6, 1, 1, 1, 9), np.array([2, 5]), np.array([4]),
                                    paid_due=np.array([3, 3]), open_due=np.array([3]))
    assert (ae, be) == (1 + 1, 9 + 1 + 2 + 2)
    assert (a, b) == (2 + 1, 6 + 2 + 2)


def test_gibbs_with_pi_zero_matches_conjugate():
    prior = TimingPrior(2, 6, 1, 1)
    k, c = np.array([2, 5, 3, 1, 1, 4]), np.array([4, 6, 2])
    post = gibbs(prior, k, c, RNG(0), n_draws=20000, fix_pi=0.0)
    _, _, a, b = conjugate_update(prior, k, c)
    assert abs(post.p.mean() - a / (a + b)) < 0.003
    assert abs(post.p.var() - a * b / ((a + b) ** 2 * (a + b + 1))) < 0.0005
    assert (post.pi == 0).all()


def test_p_dead_increases_with_age_and_tends_to_one():
    q = p_dead_given_open(np.arange(0, 200), p=0.2, pi=0.05)
    assert q[0] == pytest.approx(0.05)
    assert (np.diff(q) >= 0).all()
    assert (np.diff(q[q < 0.999]) > 0).all()  # strictly rising until it saturates in floating point
    assert q[-1] > 0.999999


def test_prediction_sums_to_one():
    post = gibbs(TimingPrior(2, 6, 1, 20), np.array([2, 3, 1, 4]), np.array([1, 7]), RNG(1))
    paid, never, beyond = predict_open(post, age=3, horizon=13)
    assert paid.sum() + never + beyond == pytest.approx(1.0)
    assert (np.diff(paid) <= 1e-12).all()  # geometric: weekly mass never rises
    # Two phases: mass jumps up at the due week.
    post2 = gibbs(TimingPrior(2, 6, 1, 20, 1, 19), np.array([5, 6]), np.array([1]), RNG(2),
                  paid_due=np.array([5, 5]), open_due=np.array([5]))
    paid2, never2, beyond2 = predict_open(post2, age=1, horizon=13, due_week=5)
    assert paid2.sum() + never2 + beyond2 == pytest.approx(1.0)
    assert paid2[3] > 2 * paid2[2]  # week 5 since issue = week 4 from now


def test_shock_hook_is_identity_at_zero_and_slows_when_negative():
    post = gibbs(TimingPrior(2, 6, 1, 20), np.array([2, 3, 1, 4]), np.array([1, 7]), RNG(1))
    base, _, _ = predict_open(post, 2, 8)
    same, _, _ = predict_open(post, 2, 8, shock_shift=0.0)
    slow, _, _ = predict_open(post, 2, 8, shock_shift=-1.0)
    assert np.array_equal(base, same)
    assert slow[0] < base[0]


def test_labels_partial_week_and_lag():
    as_of = date(2026, 3, 31)
    through = as_of - timedelta(days=3)
    out = pl.DataFrame({
        "tenant_id": ["t"] * 4,
        "invoice_id": ["paid_early", "paid_partial_week", "paid_in_lag", "open"],
        "buyer_id": ["b"] * 4,
        "issue_date": [through - timedelta(days=30)] * 4,
        "paid_date": [through - timedelta(days=20), through - timedelta(days=1), as_of - timedelta(days=1), None],
        "paid_fraction": [1.0, 1.0, 1.0, 0.0],
        "censored_at": [None, None, None, as_of],
    })
    inv = out.select("invoice_id", (pl.col("issue_date") + timedelta(days=30)).alias("due_date"))
    lab = timing_labels(out, inv, as_of).sort("invoice_id")
    by = {r["invoice_id"]: r for r in lab.iter_rows(named=True)}
    assert by["paid_early"]["state"] == "paid" and by["paid_early"]["k"] == 2
    # 30 days = 4 full weeks; a payment on day 29 is in the partial 5th week -> open at c=4.
    assert by["paid_partial_week"]["state"] == "open" and by["paid_partial_week"]["c"] == 4
    assert by["paid_in_lag"]["state"] == "open"
    assert by["open"]["c"] == 4
    assert by["open"]["d"] == 30 // 7 + 1


def test_censoring_matters():
    """Dropping open invoices keeps only the fast payers and biases p upward."""
    rng = RNG(3)
    p, pi, n = 0.15, 0.1, 3000
    issue_age = rng.integers(1, 30, size=n)  # weeks since issue at as_of
    dead = rng.random(n) < pi
    t = rng.geometric(p, size=n)
    paid = ~dead & (t <= issue_age)
    k, c = t[paid], issue_age[~paid]
    prior = TimingPrior(1, 1, 1, 1)
    full = gibbs(prior, k, c, RNG(4), n_draws=2000)
    dropped = gibbs(prior, k, np.array([], dtype=int), RNG(4), n_draws=2000)
    assert abs(full.p.mean() - p) < 0.01
    assert dropped.p.mean() > p + 0.03


def test_shrinkage_thin_vs_heavy():
    rng = RNG(5)
    gp = group_prior(np.array([0.2, 0.25, 0.3, 0.22, 0.28]), np.array([0.02, 0.03, 0.02, 0.04, 0.03]), cap=8)
    group_mean = gp.p_mean
    true_p = 0.6  # both buyers really pay much faster than the group

    def fitted(n):
        k = rng.geometric(true_p, size=n)
        return gibbs(gp, k, np.array([], dtype=int), RNG(6), n_draws=2000).p.mean()

    thin, heavy = fitted(2), fitted(200)
    assert abs(thin - group_mean) < abs(heavy - group_mean)
    assert abs(heavy - true_p) < 0.05
    assert gp.alpha + gp.beta <= 8 + 1e-9


def test_one_buyer_update_under_10ms():
    prior = TimingPrior(2, 6, 0.2, 7.8)
    k, c = RNG(7).geometric(0.25, size=150), RNG(8).integers(0, 30, size=40)
    gibbs(prior, k, c, RNG(9))  # warm up
    t = time.perf_counter()
    for _ in range(20):
        gibbs(prior, k, c, RNG(9))
    assert (time.perf_counter() - t) / 20 < 0.010


def test_fit_and_predict_on_world_is_reproducible():
    tables, _ = generate_world(WorldConfig(), past_seed=2, future_seed=0)
    obs = track_b_inputs(tables, WorldConfig().as_of)
    cfg = WorldConfig()
    a = fit_timing(obs["invoice_outcomes"], obs["invoices"], obs["buyers"], obs["complete_through"], run_id="r1").predict(13)
    b = fit_timing(obs["invoice_outcomes"], obs["invoices"], obs["buyers"], obs["complete_through"], run_id="r1").predict(13)
    validate(a, "features.invoice_timing")
    assert a.equals(b)
    sums = a.group_by("invoice_id").agg(pl.col("p_paid").sum() + pl.col("p_never").first() + pl.col("p_beyond").first())
    assert (sums["p_paid"] - 1).abs().max() < 1e-9


def test_benchmark_runs_and_beats_due_date():
    r = benchmark(range(3))
    assert r["n_invoices"] > 50
    assert r["model"]["log_score"] > r["due_date"]["log_score"]


@pytest.mark.slow
def test_recovery_coverage_near_nominal():
    """Well-specified check: 90% intervals contain the planted p and pi about 90% of the time."""
    r = recovery(WorldConfig(shock_sigma=0.0, part_payment_prob=0.0), range(100), n_draws=1000)
    assert r["n_buyers"] == 1000
    # Binomial SE at n=1000 is about 0.0095; allow about 3 SE.
    for name in ("p_early", "p", "pi"):
        assert 0.87 <= r[f"coverage_{name}"] <= 0.93, (name, r)


@pytest.mark.slow
def test_rhat_four_chains():
    r = rhat_check(WorldConfig(shock_sigma=0.0, part_payment_prob=0.0), range(5))
    for name in ("p_early", "p", "pi"):
        assert r[f"max_rhat_{name}"] < 1.01, r


def test_empirical_counts_and_shrinkage():
    from matsya_intelligence.timing.empirical import J_LO, buyer_hazard, counts, predict_open_empirical

    # due week 3: paid in week 2 (j=-1) and week 3 (j=0); open after 1 full week (at risk j=-2 only)
    e, r = counts([2, 3], [3, 3], [1], [3])
    at = lambda j: j - J_LO  # noqa: E731
    assert e[at(-1)] == 1 and e[at(0)] == 1 and e.sum() == 2
    assert (r[at(-2)], r[at(-1)], r[at(0)], r[at(1)]) == (3, 2, 1, 0)
    g = np.full_like(e, 0.3)
    h = buyer_hazard(e, r, g, 8.0)
    assert h[at(1)] == pytest.approx(0.3)  # no data: group hazard
    assert h[at(0)] == pytest.approx((1 + 2.4) / 9)
    paid, never, beyond = predict_open_empirical(h, 0.05, age=0, horizon=13, due_week=3)
    assert paid.sum() + never + beyond == pytest.approx(1.0)


def test_empirical_mode_reproducible_and_valid():
    tables, _ = generate_world(WorldConfig(), past_seed=2, future_seed=0)
    obs = track_b_inputs(tables, WorldConfig().as_of)
    args = (obs["invoice_outcomes"], obs["invoices"], obs["buyers"], obs["complete_through"])
    tm = fit_timing(*args, run_id="r1", empirical=True)
    assert tm.hazards
    a, b = tm.predict(13), fit_timing(*args, run_id="r1", empirical=True).predict(13)
    validate(a, "features.invoice_timing")
    assert a.equals(b)
    sums = a.group_by("invoice_id").agg(pl.col("p_paid").sum() + pl.col("p_never").first() + pl.col("p_beyond").first())
    assert (sums["p_paid"] - 1).abs().max() < 1e-9
