"""Polars schemas derived from the row models, and validate().

validate() raises ContractError on the first problem and never drops or fixes rows.

Fixtures and CI validate every row (sample=None). Real writes check columns, dtypes, nulls
and key uniqueness on the whole frame, and the row rules on a sample of WRITE_SAMPLE rows
(configurable per call).
"""

import types
import typing
from datetime import date, datetime
from enum import StrEnum

import polars as pl

from matsya_contracts.tables import MONEY, TABLES, Pl, Row

UTC_DTYPE = pl.Datetime("us", "UTC")
WRITE_SAMPLE = 1_000
_BASE = {str: pl.String(), int: pl.Int64(), float: pl.Float64(), bool: pl.Boolean(), date: pl.Date()}


class ContractError(ValueError):
    pass


def _unwrap(hint) -> tuple[object, tuple, bool]:
    """(base type, Annotated metadata, nullable) for a field's type hint."""
    nullable, meta = False, ()
    while True:
        origin = typing.get_origin(hint)
        if origin is typing.Annotated:
            meta += hint.__metadata__
            hint = hint.__origin__
        elif origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(hint) if a is not type(None)]
            nullable = nullable or len(args) < len(typing.get_args(hint))
            (hint,) = args
        else:
            return hint, meta, nullable


def _dtype(base, meta) -> pl.DataType:
    for m in meta:
        if isinstance(m, Pl):
            return m.dtype
    if isinstance(base, type) and issubclass(base, datetime):
        return UTC_DTYPE
    if isinstance(base, type) and issubclass(base, StrEnum):
        return pl.String()
    if typing.get_origin(base) is list:
        return pl.List(_BASE[typing.get_args(base)[0]])
    return _BASE[base]


def _model(table: str | type[Row]) -> type[Row]:
    return TABLES[table] if isinstance(table, str) else table


def fields(table: str | type[Row]) -> dict[str, tuple[pl.DataType, bool, bool]]:
    """column -> (polars dtype, nullable, is money)."""
    model = _model(table)
    hints = typing.get_type_hints(model, include_extras=True)
    out = {}
    for name in model.model_fields:
        base, meta, nullable = _unwrap(hints[name])
        out[name] = (_dtype(base, meta), nullable, any(m is MONEY for m in meta))
    return out


def schema(table: str | type[Row]) -> dict[str, pl.DataType]:
    return {c: d for c, (d, _, _) in fields(table).items()}


def empty(table: str | type[Row]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema(table))


def frame(table: str | type[Row], rows: list[Row]) -> pl.DataFrame:
    """Build a frame from already-validated row models."""
    model = _model(table)
    cols = list(model.model_fields)
    data = [{c: (v.value if isinstance(v, StrEnum) else v) for c, v in r.model_dump().items()} for r in rows]
    return pl.DataFrame(data, schema=schema(model)).select(cols)


def validate(df: pl.DataFrame, table: str | type[Row], sample: int | None = None, seed: int = 0) -> pl.DataFrame:
    """Raise ContractError unless df matches the table's contract. Returns df unchanged.

    sample=None checks the row rules on every row; an int checks them on that many rows,
    drawn with seed.
    """
    model = _model(table)
    want = fields(model)
    if df.columns != list(want):
        raise ContractError(f"{model.table}: columns {df.columns} != {list(want)}")
    for col, (dtype, nullable, _) in want.items():
        if df.schema[col] != dtype:
            raise ContractError(f"{model.table}.{col}: dtype {df.schema[col]} != {dtype}")
        if not nullable and df[col].null_count():
            raise ContractError(f"{model.table}.{col}: has nulls")
    if df.select(model.pk).is_duplicated().any():
        raise ContractError(f"{model.table}: duplicate key {model.pk}")
    rows = df if sample is None or sample >= df.height else df.sample(n=sample, seed=seed)
    for i, row in enumerate(rows.iter_rows(named=True)):
        try:
            model.model_validate(row)
        except ValueError as e:
            raise ContractError(f"{model.table} row {i}: {e}") from e
    return df
