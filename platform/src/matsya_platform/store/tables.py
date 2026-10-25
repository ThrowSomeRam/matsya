"""Local table store: Parquet files on disk, queried with DuckDB.

Layout: <root>/tables/<schema>/<name>/<tenant_id>/part-<seq>.parquet. Partitioning by
tenant makes erasure of a tenant's derived rows a directory removal rather than a rewrite.
Contract tables (matsya_contracts.tables.TABLES) are validated on every write, on every row;
the auxiliary tables build() and the matcher produce with no contract table yet (coverage,
unverified, match links, review, events, label evidence) are stored under "aux.*" as given.

append() adds a part; replace() swaps a tenant's whole partition for mutable tables such as
ops.consents. Writes go to a temporary file and are renamed into place, so a reader never
sees half a part.
"""

import shutil
from pathlib import Path

import duckdb
import polars as pl

from matsya_contracts.tables import TABLES
from matsya_contracts.validate import empty, validate


class TableStore:
    def __init__(self, root: str | Path):
        self.dir = Path(root) / "tables"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _table_dir(self, table: str) -> Path:
        schema, name = table.split(".", 1)
        return self.dir / schema / name

    def _check(self, table: str, df: pl.DataFrame, tenant_id: str) -> pl.DataFrame:
        if "tenant_id" not in df.columns:
            df = df.with_columns(pl.lit(tenant_id, pl.String).alias("tenant_id"))
        other = df.filter(pl.col("tenant_id") != tenant_id).height
        if other:
            raise ValueError(f"{table}: {other} rows belong to a tenant other than {tenant_id}")
        if table in TABLES:
            validate(df, table, sample=None)
        return df

    def _write(self, part_dir: Path, name: str, df: pl.DataFrame) -> Path:
        part_dir.mkdir(parents=True, exist_ok=True)
        p = part_dir / name
        tmp = p.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(p)
        return p

    def append(self, table: str, df: pl.DataFrame, tenant_id: str) -> Path | None:
        df = self._check(table, df, tenant_id)
        if df.is_empty():
            return None
        part_dir = self._table_dir(table) / tenant_id
        seq = len(list(part_dir.glob("part-*.parquet"))) if part_dir.exists() else 0
        return self._write(part_dir, f"part-{seq:06d}.parquet", df)

    def replace(self, table: str, df: pl.DataFrame, tenant_id: str) -> None:
        df = self._check(table, df, tenant_id)
        part_dir = self._table_dir(table) / tenant_id
        new = self._write(part_dir, "part-new.parquet", df)
        for p in part_dir.glob("part-*.parquet"):
            if p != new:
                p.unlink()
        new.replace(part_dir / "part-000000.parquet")

    def files(self, table: str, tenant_id: str | None = None) -> list[Path]:
        d = self._table_dir(table)
        return sorted(d.glob(f"{tenant_id or '*'}/part-*.parquet"))

    def read(self, table: str, tenant_id: str | None = None) -> pl.DataFrame:
        files = self.files(table, tenant_id)
        if not files:
            return empty(table) if table in TABLES else pl.DataFrame()
        df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
        return df.select(list(empty(table).columns)) if table in TABLES else df

    def tenants(self, table: str) -> list[str]:
        d = self._table_dir(table)
        return sorted(t.name for t in d.iterdir() if t.is_dir()) if d.exists() else []

    def tables(self) -> list[str]:
        return sorted(f"{s.name}.{t.name}" for s in self.dir.iterdir() if s.is_dir()
                      for t in s.iterdir() if t.is_dir() and any(t.glob("*/part-*.parquet")))

    def query(self, sql: str) -> pl.DataFrame:
        """Run SQL over every stored table, addressed as schema.name (e.g. ledger.transactions)."""
        con = duckdb.connect()
        try:
            for table in self.tables():
                schema, name = table.split(".", 1)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                glob = str(self._table_dir(table) / "*" / "part-*.parquet").replace("'", "''")
                con.execute(f"CREATE VIEW \"{schema}\".\"{name}\" AS "
                            f"SELECT * FROM read_parquet('{glob}', union_by_name = true)")
            cur = con.execute(sql)
            rows = cur.fetchall()
            return pl.DataFrame(rows, schema=[d[0] for d in cur.description], orient="row")
        finally:
            con.close()

    def erase_tenant(self, tenant_id: str, keep: tuple[str, ...] = ("ops.events",)) -> dict[str, int]:
        """Remove every stored row of the tenant, except the tables in keep (the audit trail
        of the erasure itself). Returns the rows removed per table."""
        removed = {}
        for table in self.tables():
            if table in keep:
                continue
            d = self._table_dir(table) / tenant_id
            if d.exists():
                removed[table] = sum(pl.scan_parquet(f).select(pl.len()).collect().item() for f in d.glob("*.parquet"))
                shutil.rmtree(d)
        return removed
