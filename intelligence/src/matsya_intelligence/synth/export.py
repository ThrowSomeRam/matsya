"""Publish a generator world as Parquet in the contract schema (request E from Track A).

write_contract_parquet(world, out_dir) writes:
  out_dir/<namespace>.<table>.parquet   every table Track A would produce at as_of
  out_dir/truth/<name>.parquet          generator truth (never shown to the models);
                                        truth/transactions carries bank_txn_id and day_seq
                                        so renderers can rebuild txn_uid and running balances
Every contract table is validated (all rows) before it is written.
"""

from pathlib import Path

import polars as pl
from matsya_contracts.validate import validate

from matsya_intelligence.synth.world import Truth

TRUTH_TABLES = ("sector_shocks", "weekly_orders", "buyers", "invoices", "payments", "purchases", "transactions",
                "balances", "obligations")


def write_contract_parquet(world: tuple[dict[str, pl.DataFrame], Truth], out_dir: str | Path) -> dict[str, Path]:
    tables, truth = world
    out_dir = Path(out_dir)
    (out_dir / "truth").mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, df in sorted(tables.items()):
        validate(df, name)
        paths[name] = out_dir / f"{name}.parquet"
        df.write_parquet(paths[name])
    for name in TRUTH_TABLES:
        paths[f"truth.{name}"] = out_dir / "truth" / f"{name}.parquet"
        getattr(truth, name).write_parquet(paths[f"truth.{name}"])
    return paths
