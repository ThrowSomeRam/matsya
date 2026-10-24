import numpy as np
import polars as pl

from matsya_intelligence.shocks import fit_ar1, fit_sector_shocks, kalman_filter, lateness_residuals
from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.timing import fit_timing


def test_kalman_recovers_ar1():
    rng = np.random.default_rng(0)
    rho, q, n = 0.8, 0.09, 2000
    z = np.zeros(n)
    for t in range(1, n):
        z[t] = rho * z[t - 1] + np.sqrt(q) * rng.standard_normal()
    R = np.full(n, 0.05)
    y = z + np.sqrt(R) * rng.standard_normal(n)
    y[::7] = np.nan  # missing weeks are skipped
    fit = fit_ar1(y, R)
    assert abs(fit.rho - rho) <= 0.1
    assert 0.5 * q < fit.q < 2 * q
    assert abs(fit.mean_T - z[-1]) < 3 * np.sqrt(fit.var_T)


def test_kalman_filter_no_data_returns_prior():
    m, P, ll = kalman_filter(np.full(5, np.nan), np.ones(5), 0.5, 0.3)
    assert m == 0 and ll == 0 and abs(P - 0.3 / (1 - 0.25)) < 1e-12


def test_sector_residuals_track_planted_shock():
    """With strong planted sector shocks, the filtered lateness series correlates with the true z."""
    cfg = WorldConfig(shock_sigma=0.6, n_buyers=12, order_mean_per_week=20.0, pre_due_phase=False)
    obs, truth = generate_world(cfg, past_seed=3, future_seed=0)
    tm = fit_timing(obs["invoice_outcomes"], obs["invoices"], obs["buyers"], cfg.as_of_date, run_id="s")
    means = {b: (float(p.p_early.mean()), float(p.p.mean()), float(p.pi.mean())) for b, p in tm.posteriors.items()}
    sector_of = dict(zip(obs["buyers"]["buyer_id"], obs["buyers"]["sector"]))
    res = lateness_residuals(tm.labels, means, sector_of, cfg.as_of_date)
    w0 = (cfg.as_of_date - cfg.start).days // 7
    corrs = []
    for s, (weeks, z_obs, _) in res.items():
        true = truth.sector_shocks.filter(pl.col("sector") == s).sort("week")["z"].to_numpy()
        ok = np.isfinite(z_obs) & (weeks > -45)
        # generator loadings average -1, so the logit shift is about -z_true and lateness about +z_true
        corrs.append(np.corrcoef(z_obs[ok], true[w0 + weeks[ok]])[0, 1])
    assert np.mean(corrs) > 0.4, corrs
    # Centred: no permanent lateness from never-paid invoices.
    pooled = np.concatenate([z[np.isfinite(z)] for _, z, _ in res.values()])
    assert abs(pooled.mean()) < 0.1
    shocks = fit_sector_shocks(tm.labels, means, sector_of, cfg.as_of_date)
    assert set(shocks.fits) == set(res)


def test_base_chance_shock_averaged_equals_posterior():
    """E_z[sigmoid(b - z)] = p over the long-run z distribution; the base is less extreme than p."""
    from matsya_intelligence.shocks import AR1Fit, SectorShocks, base_chance, shock_averaged

    p = np.array([0.02, 0.1, 0.3, 0.5, 0.8])
    fit = AR1Fit(rho=0.8, q=0.09, mean_T=0.0, var_T=0.09 / (1 - 0.64), loglik=0.0)
    base = base_chance(p, fit.stationary_var)
    assert np.allclose(shock_averaged(np.log(base / (1 - base)), fit.stationary_var), p, atol=1e-10)
    assert (base[p < 0.5] < p[p < 0.5]).all() and abs(base[3] - 0.5) < 1e-9
    assert np.allclose(base_chance(p, 0.0), p)
    # Monte Carlo through the engine hook: z starts at its long-run mean (0) with the
    # stationary spread, so every simulated week has the long-run distribution.
    sampler = SectorShocks({"s": fit}).shift_sampler(["s"] * len(p))
    shift = sampler(200_000, 14, np.random.default_rng(0))
    n = 200_000
    for i in range(len(p)):
        sh = shift(np.arange(n), np.full(n, i), np.full(n, 8))
        h = 1 / (1 + np.exp(-(np.log(base[i] / (1 - base[i])) + sh)))
        assert abs(h.mean() - p[i]) < 4 * h.std() / np.sqrt(n) + 1e-4, (i, h.mean(), p[i])
