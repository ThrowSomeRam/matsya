"""Load contract tables from the configured DATA_SOURCE (config/data_source.yaml).

Both sources give the same thing: dict[contract table name, DataFrame], validated
against the contract, plus the as_of to read them at. Switching between generator
worlds and Track A's real tables is the one DATA_SOURCE value.
"""

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import polars as pl
import yaml
from matsya_contracts.validate import WRITE_SAMPLE, validate

from matsya_intelligence.io.types import READS

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "data_source.yaml"


@dataclass
class Source:
    name: str  # "synth" | "contract"
    tables: dict[str, pl.DataFrame]
    as_of: date | datetime


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg["DATA_SOURCE"] = os.environ.get("MATSYA_DATA_SOURCE", cfg["DATA_SOURCE"])
    return cfg


def read_contract_dir(directory: str | Path, sample: int | None = WRITE_SAMPLE) -> dict[str, pl.DataFrame]:
    """Every table Track B reads, from <namespace>.<table>.parquet, validated on read."""
    directory = Path(directory)
    out = {}
    for table in READS:
        df = pl.read_parquet(directory / f"{table}.parquet")
        out[table] = validate(df, table, sample=sample)
    return out


def load(cfg: dict | None = None) -> Source:
    cfg = cfg or load_config()
    kind = cfg["DATA_SOURCE"]
    if kind == "synth":
        from matsya_intelligence.synth import WorldConfig, generate_world

        s = cfg["synth"]
        wc = WorldConfig.preset(s["preset"])
        tables, _ = generate_world(wc, s["past_seed"], s["future_seed"])
        for t, df in tables.items():
            validate(df, t)
        return Source("synth", tables, wc.as_of)
    if kind == "contract":
        c = cfg["contract"]
        d = Path(c["dir"])
        tables = read_contract_dir(d if d.is_absolute() else ROOT / d)
        as_of = date.fromisoformat(c["as_of"]) if c.get("as_of") else tables["ledger.transactions"]["known_at"].max().date()
        return Source("contract", tables, as_of)
    raise ValueError(f"unknown DATA_SOURCE {kind!r} (synth | contract)")
