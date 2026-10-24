"""Write Track B's output tables. Every frame is validated against its contract first."""

from pathlib import Path

import polars as pl
from matsya_contracts.tables import TABLES
from matsya_contracts.validate import WRITE_SAMPLE, validate

TRACK_B_NAMESPACES = ("features", "forecast", "decisions")


def write_outputs(frames: dict[str, pl.DataFrame], out_dir: str | Path, sample: int | None = WRITE_SAMPLE) -> list[Path]:
    """One <namespace>.<table>.parquet per frame. Refuses tables outside Track B's namespaces."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for table, df in sorted(frames.items()):
        if table.split(".")[0] not in TRACK_B_NAMESPACES or TABLES[table].writer != "B":
            raise ValueError(f"Track B does not write {table}")
        validate(df, table, sample=sample)
        path = out_dir / f"{table}.parquet"
        df.write_parquet(path)
        paths.append(path)
    return paths
