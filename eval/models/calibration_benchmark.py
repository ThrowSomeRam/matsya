"""Calibration benchmark on generator worlds (report only: gamma is not chosen here for production).

    uv run --project intelligence python eval/models/calibration_benchmark.py

Each world is replayed day by day from START to the end of its simulated future (about 300
steps, 2,000 paths) with live calibration (online method), keeping each step's simulated
balances at the scored horizons. The engine's paths do not depend on calibration, so every
method is then replayed offline on the same paths with the same delayed feedback: "off"
(the engine's own 80% band), ACI at each gamma in the constants grid, the online step-size
method, and split conformal (CQR-style: widen the nominal band by the 80% quantile of
max(lo - y, y - hi) over a fixed calibration set, never updated). The offline online-method
replay must reproduce the live run exactly (checked).

1. Three borderline worlds (past seeds 0, 1, 2): coverage of the 80% band per horizon, outcomes
   with target date >= EVAL_FROM (warm-up excluded).
2. Shock world: past seed 0 with every buyer's weekly payment chances x 0.3 from SHOCK_AT.
   Weekly coverage by target week; weeks to recover = weeks after the shock until trailing
   4-week coverage is back >= 0.75 after having fallen below it. Split conformal is calibrated
   on outcomes with target date before the shock.
"""

import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import results  # noqa: E402

from matsya_intelligence.calibrate import Calibrator, read_band  # noqa: E402
from matsya_intelligence.calibrate.aci import constants  # noqa: E402

START = datetime(2025, 10, 1, 18, tzinfo=timezone.utc)
EVAL_FROM = date(2026, 1, 1)
SHOCK_AT = date(2026, 2, 2)
SHOCK_FACTOR = 0.3
N_PATHS = 2_000
WORLDS = {"borderline_0": (0, None), "borderline_1": (1, None), "borderline_2": (2, None),
          "shock_0": (0, (SHOCK_AT, SHOCK_FACTOR))}


def _replay(name: str) -> dict:
    import os

    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    from matsya_intelligence.synth import WorldConfig, generate_world
    from matsya_intelligence.synth.replay import SCORE_HORIZONS, Replay

    seed, shock = WORLDS[name]
    cfg = replace(WorldConfig(), payment_shock=shock)
    _, truth = generate_world(cfg, seed, 0)
    r = Replay(truth, n_paths=N_PATHS, start=START, keep_paths=True, calibrator=Calibrator(method="online"))
    r.run((r.last_day - START).days)
    live = pl.concat([s.calibration["scored"] for s in r.steps if s.calibration["scored"].height])
    return {"name": name, "days": [s.as_of.date() for s in r.steps], "paths": [s.paths for s in r.steps],
            "horizons": SCORE_HORIZONS, "truth": dict(zip(truth.balances["date"], truth.balances["closing_paise"])),
            "live": live}


def _offline(run: dict, cal: Calibrator) -> pl.DataFrame:
    """Same cadence as the replay: on each step day, score what is known (targets <= day), then issue."""
    hs = run["horizons"]
    out = []
    for day, paths in zip(run["days"], run["paths"]):
        known = {d: v for d, v in run["truth"].items() if d <= day}
        s = cal.update(day, known)
        if s.height:
            out.append(s)
        full = np.zeros((paths.shape[0], max(hs) + 1))
        full[:, list(hs)] = paths
        cal.issue(day, full)
    return pl.concat(out)


def _split_conformal(run: dict, alpha: float, cal_until: date | None) -> pl.DataFrame:
    """Nominal band widened by a fixed margin from a calibration set: targets before cal_until
    (or the first 60 days of outcomes); before the margin exists the nominal band is used."""
    hs = run["horizons"]
    issued = []
    for day, paths in zip(run["days"], run["paths"]):
        for j, h in enumerate(hs):
            lo, hi = read_band(paths[:, j], alpha)
            issued.append((h, day, day + timedelta(days=h), lo, hi))
    df = pl.DataFrame(issued, schema={"horizon": pl.Int32, "made_on": pl.Date, "target_date": pl.Date,
                                      "lo": pl.Float64, "hi": pl.Float64}, orient="row")
    df = df.with_columns(pl.col("target_date").replace_strict(run["truth"], default=None, return_dtype=pl.Float64)
                         .alias("actual")).drop_nulls("actual")
    df = df.with_columns(pl.max_horizontal(pl.col("lo") - pl.col("actual"), pl.col("actual") - pl.col("hi")).alias("score"))
    first = run["days"][0]
    cut = cal_until or (first + timedelta(days=60))
    out = []
    for h in hs:
        d = df.filter(pl.col("horizon") == h)
        calset = d.filter(pl.col("target_date") < cut)["score"].to_numpy()
        n = calset.size
        q = np.quantile(calset, min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)) if n else 0.0
        # the margin exists only once the calibration targets are known: from `cut` on
        late = pl.col("made_on") >= cut
        out.append(d.with_columns(
            pl.when(late).then(pl.col("lo") - q).otherwise(pl.col("lo")).alias("lo"),
            pl.when(late).then(pl.col("hi") + q).otherwise(pl.col("hi")).alias("hi")))
    d = pl.concat(out)
    return d.with_columns(((pl.col("actual") < pl.col("lo")) | (pl.col("actual") > pl.col("hi"))).cast(pl.Int8)
                          .alias("miss")).select("horizon", "made_on", "target_date", "miss")


def methods(run: dict, cal_until: date | None) -> dict[str, pl.DataFrame]:
    c = constants()
    out = {"off": _offline(run, Calibrator(method="off"))}
    for g in c["aci_gamma"]["grid"]:
        out[f"aci_gamma_{g}"] = _offline(run, Calibrator(method="aci", gamma=g))
    out["online"] = _offline(run, Calibrator(method="online"))
    out["split_conformal"] = _split_conformal(run, 1 - float(c["target_coverage"]["value"]), cal_until)
    live = run["live"].select("horizon", "made_on", "target_date", "miss").sort("horizon", "target_date", "made_on")
    mine = out["online"].select("horizon", "made_on", "target_date", "miss").sort("horizon", "target_date", "made_on")
    assert live.equals(mine), "offline online-method replay differs from the live replay"
    return {k: v.select("horizon", "made_on", "target_date", "miss") for k, v in out.items()}


def _weekly(m: pl.DataFrame, h: int) -> pl.DataFrame:
    return (m.filter(pl.col("horizon") == h).group_by(pl.col("target_date").dt.truncate("1w").alias("week"))
            .agg((1 - pl.col("miss").mean()).alias("coverage"), pl.len().alias("n")).sort("week"))


def _recovery(m: pl.DataFrame, h: int, target: float = 0.75) -> tuple[float | None, float, int]:
    """(weeks from the shock until trailing 4-week coverage is back >= target after falling below
    it, or None if never; minimum trailing coverage after the shock; n outcomes after the shock)."""
    w = _weekly(m, h).with_columns(
        (pl.col("coverage") * pl.col("n")).rolling_sum(4).truediv(pl.col("n").rolling_sum(4)).alias("trail"))
    after = w.filter(pl.col("week") >= SHOCK_AT)
    if after.is_empty():
        return None, float("nan"), 0
    fell = False
    for wk, tr in zip(after["week"], after["trail"]):
        fell = fell or (tr is not None and tr < target)
        if fell and tr is not None and tr >= target:
            return (wk - SHOCK_AT).days / 7, float(after["trail"].min()), int(after["n"].sum())
    return (None if fell else 0.0), float(after["trail"].min()), int(after["n"].sum())


def main() -> None:
    import multiprocessing as mp

    with ProcessPoolExecutor(len(WORLDS), mp_context=mp.get_context("spawn")) as ex:
        runs = {r["name"]: r for r in ex.map(_replay, WORLDS)}
    rows, md = [], ["# Calibration benchmark (generator worlds, report only)\n",
                    f"Replays from {START.date()} to the end of each world's future, {N_PATHS:,} paths, daily steps; "
                    f"target coverage 0.80. Gamma is not chosen here for production.\n"]
    # 1. three borderline worlds
    per = {name: methods(runs[name], None) for name in ("borderline_0", "borderline_1", "borderline_2")}
    names = list(next(iter(per.values())))
    table = []
    for h in runs["borderline_0"]["horizons"]:
        rec = {"horizon_days": h}
        for mname in names:
            ms = pl.concat([per[w][mname] for w in per]).filter(
                (pl.col("horizon") == h) & (pl.col("target_date") >= EVAL_FROM))
            cov, n = 1 - ms["miss"].mean() if ms.height else None, ms.height
            rec[mname] = cov
            rec["n"] = n
            rows.append(results.row("calibration_coverage", "generator_borderline_x3", "calibration", mname,
                                    "coverage_80", cov, n, "off", horizon=f"{h}d",
                                    note=f"targets >= {EVAL_FROM}, worlds past seeds 0-2"))
        table.append(rec)
    md.append(f"## Coverage of the 80% band, three borderline worlds (targets from {EVAL_FROM})\n\n"
              "'off' is the engine's own band (no calibration); n = scored outcomes per horizon over the "
              "three worlds (the same n for every method).\n\n" + results.md_table(pl.DataFrame(table)))
    # 2. shock world
    shock = methods(runs["shock_0"], SHOCK_AT)
    rec_rows = []
    for h in (7, 14, 30):
        for mname in ("off", "aci_gamma_0.005", "aci_gamma_0.05", "online", "split_conformal"):
            wk, mn, n = _recovery(shock[mname], h)
            rec_rows.append({"horizon_days": h, "method": mname, "weeks_to_recover": wk,
                             "min_trailing_4w_coverage": mn, "n_after_shock": n})
            rows.append(results.row("calibration_shock", "generator_shock_x0.3", "calibration", mname,
                                    "weeks_to_recover", wk, n, "split_conformal", horizon=f"{h}d",
                                    note=f"shock {SHOCK_AT} p x {SHOCK_FACTOR}; trailing 4w coverage >= 0.75; "
                                         f"min trailing {mn:.3f}" + ("; never recovered" if wk is None else "")))
    weekly = []
    for mname in ("off", "aci_gamma_0.05", "online", "split_conformal"):
        w = _weekly(shock[mname], 14).rename({"coverage": mname})
        weekly.append(w.select("week", mname, "n"))
    wk = weekly[0]
    for w in weekly[1:]:
        wk = wk.join(w.drop("n"), on="week")
    md.append(f"\n\n## Shock: payment chances x {SHOCK_FACTOR} from {SHOCK_AT} (world past seed 0)\n\n"
              "Weeks to recover: weeks after the shock until trailing 4-week coverage is back >= 0.75 after falling "
              "below it (blank = never fell below, None = never recovered within the world). Split conformal is "
              "calibrated on targets before the shock and not updated.\n\n" + results.md_table(pl.DataFrame(rec_rows))
              + "\n\n### Weekly coverage, 14-day horizon (by target week; n per week)\n\n"
              + results.md_table(wk.filter(pl.col("week") >= SHOCK_AT - timedelta(weeks=6))))
    results.write(rows, "\n".join(md) + "\n", "calibration")
    print("\n".join(md))


if __name__ == "__main__":
    main()
