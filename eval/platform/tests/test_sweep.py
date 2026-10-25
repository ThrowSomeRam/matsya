import polars as pl

from render.sweep import summarise, sweep
from test_render import make_world


def test_sweep_is_deterministic_and_summarises(tmp_path):
    world = make_world(tmp_path / "world")
    args = ([str(world)], [0, 50, 100], [0, 1, 2], ["drop_all_ids", "partial_window"])
    a = sweep(*args, work=tmp_path / "w1", workers=4)
    b = sweep(*args, work=tmp_path / "w2", workers=2)
    closed = sweep(*args, work=tmp_path / "w3", workers=4, loops=("closed",))
    assert closed.filter(pl.col("verdict") == "ERROR").height == 0 and set(closed["loop"]) == {"closed"}
    assert a.equals(b)  # deterministic, whatever the parallelism
    assert set(a["fault"]) == {"drop_all_ids", "partial_window", "combined", "no_anchors"} and a.height == 36
    assert a.filter(pl.col("verdict") == "ERROR").height == 0
    levels, thresholds = summarise(a)
    assert levels.height == 12 and set(levels["loop"]) == {"open"} and set(thresholds["fault"]) == {"drop_all_ids", "partial_window", "combined", "no_anchors"}
    full = a.filter((pl.col("fault") == "drop_all_ids") & (pl.col("level_pct") == 100))
    assert (full["key_ord"] == full["ledger_rows"]).all()
