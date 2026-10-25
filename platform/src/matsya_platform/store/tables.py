"""Local table store: encrypted Parquet files on disk, queried with DuckDB.

Layout: <root>/tables/<schema>/<name>/<partition>/part-<seq>.<k>.enc, where partition is the
tenant id, or _shared for contract tables with no tenant (ref.buyers, ref.buyer_disclosures).
Partitioning by tenant makes erasure of a tenant's rows a directory removal.

Every part is a Parquet file encrypted with AES-256-GCM (random 96-bit nonce, stored as nonce
|| ciphertext), with "<table>|<partition>" as associated data, so a part copied to another
table or tenant does not decrypt. <k> names the key:
- t: the partition's tenant key. Every tenant row, derived or not, is under it, so destroying
  the tenant key makes every one of its table files noise, as it does its raw payloads.
- p: the platform key (keys.PLATFORM). Used for the shared reference tables and for the rows a
  tenant's erasure must not take with it: the consent record and its lifecycle events.
No plaintext Parquet is ever written under <root>.

Reads decrypt to memory. DuckDB cannot read encrypted Parquet, and without pyarrow it cannot
be handed in-memory frames either, so query() decrypts only the tables the SQL names into a
temporary directory (in /dev/shm when it exists, so the plaintext sits in RAM, not on disk),
runs the query, and removes the directory whatever happens.

Contract tables are validated on every write, on every row; the auxiliary tables with no
contract table yet are stored under "aux.*" as given. append() adds a part; replace() swaps a
partition for mutable tables such as ops.consents.
"""

import io
import os
import re
import shutil
import tempfile
from pathlib import Path

import duckdb
import polars as pl
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from matsya_contracts.tables import TABLES, TenantRow
from matsya_contracts.validate import empty, validate
from matsya_platform.store.keys import PLATFORM, KeyStore, Unreadable

SHARED = "_shared"
_PART = re.compile(r"^part-(\d+)\.([tp])\.enc$")


def tenant_scoped(table: str) -> bool:
    """Every auxiliary table is per tenant; a contract table is when its model has tenant_id."""
    return table not in TABLES or issubclass(TABLES[table], TenantRow)


class TableStore:
    def __init__(self, root: str | Path, keys: KeyStore | None = None):
        self.dir = Path(root) / "tables"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keys = keys if keys is not None else KeyStore(root)

    def _table_dir(self, table: str) -> Path:
        schema, name = table.split(".", 1)
        return self.dir / schema / name

    def _check(self, table: str, df: pl.DataFrame, tenant_id: str | None) -> pl.DataFrame:
        if not tenant_scoped(table):
            if table in TABLES:
                validate(df, table, sample=None)
            return df
        if tenant_id is None:
            raise ValueError(f"{table} is per tenant: a tenant_id is required")
        if "tenant_id" not in df.columns:
            df = df.with_columns(pl.lit(tenant_id, pl.String).alias("tenant_id"))
        other = df.filter(pl.col("tenant_id") != tenant_id).height
        if other:
            raise ValueError(f"{table}: {other} rows belong to a tenant other than {tenant_id}")
        if table in TABLES:
            validate(df, table, sample=None)
        return df

    def _partition(self, table: str, tenant_id: str | None) -> str:
        return tenant_id if tenant_scoped(table) else SHARED

    def _key_id(self, table: str, platform: bool) -> str:
        return "p" if platform or not tenant_scoped(table) else "t"

    def _key(self, partition: str, k: str, create: bool) -> bytes:
        return self.keys.tenant_key(PLATFORM if k == "p" else partition, create=create)

    @staticmethod
    def _aad(table: str, partition: str) -> bytes:
        return f"{table}|{partition}".encode()

    def _write(self, table: str, partition: str, name: str, k: str, df: pl.DataFrame) -> Path:
        buf = io.BytesIO()
        df.write_parquet(buf)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._key(partition, k, create=True)).encrypt(nonce, buf.getvalue(),
                                                                           self._aad(table, partition))
        part_dir = self._table_dir(table) / partition
        part_dir.mkdir(parents=True, exist_ok=True)
        p = part_dir / name
        tmp = part_dir / f".{name}.tmp"
        tmp.write_bytes(blob)
        tmp.replace(p)
        return p

    def _decrypt(self, table: str, p: Path) -> pl.DataFrame:
        partition, k = p.parent.name, _PART.match(p.name).group(2)
        blob = p.read_bytes()
        try:
            key = self._key(partition, k, create=False)
        except Exception as e:
            raise Unreadable(f"{table}/{partition}/{p.name}: {e}") from e
        try:
            data = AESGCM(key).decrypt(blob[:12], blob[12:], self._aad(table, partition))
        except InvalidTag as e:
            raise Unreadable(f"{table}/{partition}/{p.name}: authentication failed") from e
        return pl.read_parquet(io.BytesIO(data))

    def _parts(self, part_dir: Path) -> list[Path]:
        return sorted(p for p in part_dir.glob("part-*.enc") if _PART.match(p.name)) if part_dir.exists() else []

    def append(self, table: str, df: pl.DataFrame, tenant_id: str | None = None, *,
               platform_key: bool = False) -> Path | None:
        df = self._check(table, df, tenant_id)
        if df.is_empty():
            return None
        partition = self._partition(table, tenant_id)
        parts = self._parts(self._table_dir(table) / partition)
        seq = int(_PART.match(parts[-1].name).group(1)) + 1 if parts else 0
        k = self._key_id(table, platform_key)
        return self._write(table, partition, f"part-{seq:06d}.{k}.enc", k, df)

    def replace(self, table: str, df: pl.DataFrame, tenant_id: str | None = None, *,
                platform_key: bool = False) -> None:
        df = self._check(table, df, tenant_id)
        partition = self._partition(table, tenant_id)
        k = self._key_id(table, platform_key)
        old = self._parts(self._table_dir(table) / partition)
        new = self._write(table, partition, f"part-999999.{k}.enc", k, df)
        for p in old:
            if p != new:
                p.unlink()
        new.replace(new.with_name(f"part-000000.{k}.enc"))

    def files(self, table: str, tenant_id: str | None = None) -> list[Path]:
        d = self._table_dir(table)
        if tenant_id and tenant_scoped(table):
            return self._parts(d / tenant_id)
        return sorted(p for part in (d.iterdir() if d.exists() else []) if part.is_dir() for p in self._parts(part))

    def read(self, table: str, tenant_id: str | None = None) -> pl.DataFrame:
        files = self.files(table, tenant_id)
        if not files:
            return empty(table) if table in TABLES else pl.DataFrame()
        df = pl.concat([self._decrypt(table, f) for f in files], how="diagonal_relaxed")
        return df.select(list(empty(table).columns)) if table in TABLES else df

    def tenants(self, table: str) -> list[str]:
        d = self._table_dir(table)
        return sorted(t.name for t in d.iterdir() if t.is_dir() and t.name != SHARED) if d.exists() else []

    def tables(self) -> list[str]:
        return sorted(f"{s.name}.{t.name}" for s in self.dir.iterdir() if s.is_dir()
                      for t in s.iterdir() if t.is_dir() and any(t.glob("*/part-*.enc")))

    def query(self, sql: str) -> pl.DataFrame:
        """Run SQL over the stored tables, addressed as schema.name (e.g. ledger.transactions).
        Only the tables named in the SQL are decrypted, into a temporary directory that is
        removed when the query returns or fails."""
        named = [t for t in self.tables() if re.search(rf"\b{re.escape(t)}\b", sql)]
        tmp = tempfile.mkdtemp(prefix="matsya-q-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        con = duckdb.connect()
        try:
            for table in named:
                schema, name = table.split(".", 1)
                path = Path(tmp) / f"{table}.parquet"
                self.read(table).write_parquet(path)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                con.execute(f"CREATE VIEW \"{schema}\".\"{name}\" AS "
                            f"SELECT * FROM read_parquet('{str(path).replace(chr(39), chr(39) * 2)}')")
            cur = con.execute(sql)
            rows = cur.fetchall()
            return pl.DataFrame(rows, schema=[d[0] for d in cur.description], orient="row")
        finally:
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def erase_tenant(self, tenant_id: str, keep: tuple[str, ...] = ("ops.events",)) -> dict[str, int]:
        """Remove every stored part of the tenant. In the tables in keep, only the parts under
        the platform key stay (the record of the consent and its erasure); the tenant-key
        parts go. Returns the files removed per table. Run after the key is destroyed, every
        removed part was already noise; this removes the noise too."""
        removed = {}
        for table in self.tables():
            d = self._table_dir(table) / tenant_id
            if not d.exists():
                continue
            if table in keep:
                gone = [p for p in self._parts(d) if _PART.match(p.name).group(2) == "t"]
                for p in gone:
                    p.unlink()
            else:
                gone = self._parts(d)
                shutil.rmtree(d)
            if gone:
                removed[table] = len(gone)
        return removed
