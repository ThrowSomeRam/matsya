from datetime import date, timedelta

import numpy as np
import pytest
from matsya_contracts.validate import validate

from matsya_intelligence.calibrate import Calibrator, read_band, realised_level

D0 = date(2026, 1, 1)
PATHS = np.random.default_rng(0).normal(0, 100, (2000, 91))


def _stream(cal: Calibrator, y: float, days: int, h: int = 1) -> list[float]:
    """Issue a band every day and score it once known (outcome y on every day)."""
    levels = []
    for t in range(days):
        day = D0 + timedelta(days=t)
        cal.update(day, {day: y})
        cal.issue(day, PATHS)
        levels.append(cal.level(h))
    return levels


@pytest.mark.parametrize("method", ["aci", "online"])
def test_always_miss_widens_always_hit_narrows(method):
    miss = Calibrator(method=method, gamma=0.05, horizons=(1,))
    lv = _stream(miss, 1e6, 60)  # far outside every band
    # Falls until the band is the whole line (level <= 0), then hovers there: a band that
    # covers everything is a hit, so ACI steps back up and oscillates around 0.
    k = next(i for i, v in enumerate(lv) if v <= 0)
    assert all(b < a for a, b in zip(lv[:k], lv[1:k + 1]))
    assert max(lv[k:]) <= 0.05 + 1e-9
    lo, hi = read_band(PATHS[:, 1], lv[5])
    nlo, nhi = read_band(PATHS[:, 1], miss.alpha)
    assert lo <= nlo and hi >= nhi and hi - lo > nhi - nlo  # wider than the nominal 80% band

    hit = Calibrator(method=method, gamma=0.05, horizons=(1,))
    lv = _stream(hit, float(np.median(PATHS[:, 1])), 60)  # dead centre: always covered
    assert lv[-1] > hit.alpha
    lo, hi = read_band(PATHS[:, 1], lv[-1])
    assert hi - lo < nhi - nlo  # narrower


def test_aci_update_rule_exact():
    cal = Calibrator(method="aci", gamma=0.1, horizons=(1,))
    _stream(cal, 1e6, 3)  # issue d0, d1, d2; scored d1 (made d0) and d2 (made d1): two misses
    assert cal.state[1].alpha_t == pytest.approx(0.2 + 2 * 0.1 * (0.2 - 1))


def test_delayed_feedback_waits_for_the_outcome():
    cal = Calibrator(method="aci", gamma=0.1, horizons=(7,))
    cal.issue(D0, PATHS)
    target = D0 + timedelta(days=7)
    # Even if a value for the target day were offered early, it is not used before the day.
    for t in range(1, 7):
        s = cal.update(D0 + timedelta(days=t), {target: 1e6})
        assert s.height == 0 and cal.state[7].alpha_t == pytest.approx(0.2)
    s = cal.update(target, {})  # the day arrives but its balance is not known yet
    assert s.height == 0 and len(cal.state[7].pending) == 1
    s = cal.update(target + timedelta(days=1), {target: 1e6})
    assert s.height == 1 and s["miss"][0] == 1 and cal.state[7].alpha_t == pytest.approx(0.2 + 0.1 * (0.2 - 1))
    assert len(cal.state[7].pending) == 0


@pytest.mark.parametrize("method", ["aci", "online"])
def test_long_horizons_keep_the_engine_band(method):
    cal = Calibrator(method=method, gamma=0.1, horizons=(30, 60))
    for t in range(120):
        day = D0 + timedelta(days=t)
        cal.update(day, {day - timedelta(days=k): 1e6 for k in range(3)})  # every outcome a miss
        cal.issue(day, PATHS)
    assert cal.level(30) < cal.alpha  # adapted
    assert cal.level(60) == cal.alpha and cal.state[60].alpha_t == pytest.approx(cal.alpha)
    assert len(cal.state[60].history) > 0 and all(e == 1 for _, e in cal.state[60].history)  # still scored
    assert Calibrator(method=method, horizons=(60,), adapt_max_horizon=90).adapts(60)


def test_online_weights_stay_a_distribution():
    cal = Calibrator(method="online", horizons=(1,))
    _stream(cal, 150.0, 40)
    w = cal.state[1].weights
    assert w.sum() == pytest.approx(1.0) and (w > 0).all()


def test_realised_level_matches_band_coverage():
    p = PATHS[:, 30]
    for y in (-150.0, -20.0, 0.0, 90.0):
        beta = realised_level(p, y)
        lo, hi = read_band(p, max(beta - 0.01, 1e-6))
        assert lo <= y <= hi


def test_state_table_validates():
    cal = Calibrator(method="aci", gamma=0.5, horizons=(1, 7))
    _stream(cal, 1e6, 10)
    assert min(st.alpha_t for st in cal.state.values()) < 0  # raw level goes negative ...
    tbl = cal.state_table("5f0c6a1e-2b7d-4c3a-9e8f-1a2b3c4d5e6f", D0 + timedelta(days=9))
    validate(tbl, "forecast.calibration_state")  # ... the contract row is clipped to [0, 1]
    assert set(tbl["horizon"]) == {1, 7} and tbl["alpha_t"].is_between(0, 1).all()
    assert (tbl.filter(tbl["horizon"] == 7)["miss_rate_28d"] == 1.0).all()  # 7-day: both outcomes missed


def test_replay_with_calibration_is_deterministic_and_never_looks_ahead():
    from matsya_intelligence.synth import WorldConfig, generate_world
    from matsya_intelligence.synth.replay import Replay

    cfg = WorldConfig()
    _, truth = generate_world(cfg, 0, 0)
    start = cfg.as_of - timedelta(days=10)
    runs = []
    for _ in range(2):
        r = Replay(truth, n_paths=300, start=start, calibrator=Calibrator(method="online", horizons=(1, 7)))
        r.run(9)
        runs.append(r)
    a, b = runs
    for sa, sb in zip(a.steps, b.steps):
        for k in ("scored", "bands", "forecast.calibration_state"):
            assert sa.calibration[k].equals(sb.calibration[k]), k
        for k in sa.forecast:
            assert sa.forecast[k].equals(sb.forecast[k]), k
    scored = [s for s in a.steps if s.calibration["scored"].height]
    assert scored  # horizon 1 and 7 outcomes arrived within 9 steps
    for s in a.steps:
        sc = s.calibration["scored"]
        if sc.height:
            assert (sc["target_date"] <= s.as_of.date()).all()
            assert (sc["made_on"] < s.as_of.date()).all()
        validate(s.calibration["forecast.calibration_state"], "forecast.calibration_state")
