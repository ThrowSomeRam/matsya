"""Replay one borderline world for 60 days at benchmark paths; save raw quantiles, score, buyer range.

    uv run --project intelligence python eval/models/replay/run_world.py PAST_SEED OUT_DIR
"""
import sys
import time
from pathlib import Path

import polars as pl

from matsya_intelligence.synth import WorldConfig, generate_world
from matsya_intelligence.synth.replay import PATHS, Replay, buyer_range, score

seed = int(sys.argv[1])
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
_, truth = generate_world(WorldConfig.preset("borderline"), seed, 0)
r = Replay(truth, n_paths=PATHS["benchmark"])
t = time.time()


def show(s):
    print(f"seed {seed} {s.as_of.date()} {time.time() - t:.0f}s weeks {s.buyer['weeks_q05']:.2f}-{s.buyer['weeks_q95']:.2f}", flush=True)


r.run(60, on_step=show)
pl.concat([s.forecast["forecast.balance_quantiles"].with_columns(pl.lit(s.as_of.date()).alias("as_of"))
           for s in r.steps]).write_parquet(out / f"bq_{seed}.parquet")
truth.balances.write_parquet(out / f"truth_bal_{seed}.parquet")
score(r).write_parquet(out / f"score_{seed}.parquet")
buyer_range(r).write_parquet(out / f"buyer_{seed}.parquet")
print("done", seed, flush=True)
