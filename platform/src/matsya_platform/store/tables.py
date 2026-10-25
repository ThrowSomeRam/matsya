"""Table store: encrypted Parquet per tenant, written and read in place by DuckDB, with history.

Layout: tables/<schema>/<name>/<partition>/<kind>-<seq>-<commit ns>.<k>.parquet in an object
store (objects.py: a local directory or an S3 bucket such as MinIO), where partition is the
tenant id, or _shared for contract tables with no tenant (ref.buyers, ref.buyer_disclosures).

Every part is a Parquet file under Parquet modular encryption (AES-GCM, footer and every
column encrypted), keyed by <k>:
- t: the partition's tenant key. Every tenant row, derived or not, is under it, so destroying
  the tenant key makes every one of its table parts noise, current or historical, as it does
  its raw payloads.
- p: the platform key (keys.PLATFORM). Used for the shared reference tables and for the rows a
  tenant's erasure must not take with it: the consent record and its lifecycle events.
No plaintext Parquet is ever written, to the object store or to a temporary file: DuckDB takes
the frame from memory (Arrow C stream, no pyarrow) and writes the encrypted file, and reads
the parts straight from their URLs with the key handed to it in memory. A query reads only the
row groups and columns it needs, from disk or over S3.

History, for reproducible reads. append() adds a part; replace() adds a snapshot ("snap"); a
read takes the latest snapshot and the parts after it. Nothing a replace supersedes is
deleted, and every object name carries its commit time, so read(..., as_of=t) and
query(..., as_of=t) take only what was committed by t and return exactly what a read at t
returned. An export made at t (export.py) can be made again. Commit times are wall clock,
one writer per store assumed.

History is removed in exactly two places, and each leaves a purge marker
(<partition>/purged-<ns>): a tenant's erasure, and replace(..., purge=True), used when a
consent is revoked while the tenant has another live one. A read as of a time before a
partition's purge raises Unreadable rather than return less than was there.

Unlike a raw payload, a part does not carry its table and tenant as associated data (DuckDB
does not expose an AAD prefix). A part moved to another tenant does not decrypt there, since
the key differs, and a read checks every row's tenant_id against the partition; a part moved
between two tables of the same tenant would decrypt, and is caught only by the schema check.

Contract tables are validated on every write, on every row, and cast back to the contract
schema on read (DuckDB returns enums as strings); the auxiliary tables with no contract table
yet are stored under "aux.*" as given.
"""

import base64
import hashlib
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl

from matsya_contracts.tables import TABLES, TenantRow
from matsya_contracts.validate import empty, validate
from matsya_platform.store.keys import PLATFORM, KeyError_, KeyStore, Unreadable
from matsya_platform.store.objects import ObjectStore, open_objects

SHARED = "_shared"
_PART = re.compile(r"^(part|snap)-(\d+)-(\d{20})\.([tp])\.parquet$")
_PURGE = re.compile(r"^purged-(\d{20})$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def tenant_scoped(table: str) -> bool:
    """Every auxiliary table is per tenant; a contract table is when its model has tenant_id."""
    return table not in TABLES or issubclass(TABLES[table], TenantRow)


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ns(t: datetime) -> int:
    if t.tzinfo is None:
        raise ValueError("as_of must be timezone-aware (UTC)")
    return (t - _EPOCH) // timedelta(microseconds=1) * 1000


def _at(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat()


class _Stream:
    """A polars frame as an Arrow C stream, which DuckDB scans without pyarrow."""

    def __init__(self, df: pl.DataFrame):
        self.df = df

    def __arrow_c_stream__(self, requested_schema=None):
        return self.df.__arrow_c_stream__(requested_schema)


class TableStore:
    def __init__(self, root: str | Path, keys: KeyStore | None = None, objects: ObjectStore | None = None):
        self.keys = keys if keys is not None else KeyStore(root)
        self.backend = objects if objects is not None else open_objects(root)
        self._con: duckdb.DuckDBPyConnection | None = None
        self._con_epoch = -1
        self._registered: set[str] = set()
        self._last_ns = 0

    # ------------------------------------------------------------ duckdb and keys

    def _connection(self) -> duckdb.DuckDBPyConnection:
        # Keys handed to DuckDB stay in its memory; a destroyed key must not outlive the
        # destroy there, so any destroy since the connection opened means a new connection.
        if self._con is None or self._con_epoch != self.keys.epoch:
            if self._con is not None:
                self._con.close()
            self._con = duckdb.connect()
            self._con.execute("SET TimeZone = 'UTC'")
            self.backend.configure(self._con)
            self._con_epoch, self._registered = self.keys.epoch, set()
        return self._con

    def _key(self, partition: str, k: str, create: bool) -> bytes:
        return self.keys.tenant_key(PLATFORM if k == "p" else partition, create=create)

    def _key_name(self, partition: str, k: str, create: bool = False) -> str:
        """Hand the key to DuckDB and return the name SQL refers to it by. The name is a hash
        of the key, so a regenerated key never resolves to an old one."""
        con = self._connection()
        try:
            key = self._key(partition, k, create)
        except KeyError_ as e:
            raise Unreadable(f"{partition} ({k}): {e}") from e
        name = "k" + hashlib.sha256(key).hexdigest()[:24]
        if name not in self._registered:
            con.execute(f"PRAGMA add_parquet_key('{name}', '{base64.b64encode(key).decode()}')")
            self._registered.add(name)
        return name

    # ------------------------------------------------------------ layout

    def _prefix(self, table: str, partition: str | None = None) -> str:
        schema, name = table.split(".", 1)
        return f"tables/{schema}/{name}/" + (f"{partition}/" if partition else "")

    def _partition(self, table: str, tenant_id: str | None) -> str:
        return tenant_id if tenant_scoped(table) else SHARED

    def _key_id(self, table: str, platform: bool) -> str:
        return "p" if platform or not tenant_scoped(table) else "t"

    def _commit_ns(self) -> int:
        self._last_ns = max(time.time_ns(), self._last_ns + 1)
        return self._last_ns

    def _all(self, table: str, partition: str) -> tuple[list[tuple[int, str, str, str, int]], list[int]]:
        """(seq, kind, k, object key, commit ns) of every part and snapshot in the partition,
        superseded or not, and the commit ns of every purge."""
        parts, purges = [], []
        for key in self.backend.list_keys(self._prefix(table, partition)):
            name = key.rsplit("/", 1)[-1]
            if m := _PART.match(name):
                parts.append((int(m.group(2)), m.group(1), m.group(4), key, int(m.group(3))))
            elif m := _PURGE.match(name):
                purges.append(int(m.group(1)))
        return sorted(parts), sorted(purges)

    def _live(self, table: str, partition: str, as_of: datetime | None = None) -> list[tuple[int, str, str, str, int]]:
        """The parts a read sees: now, or as committed by as_of."""
        parts, purges = self._all(table, partition)
        if as_of is not None:
            cut = _ns(as_of)
            if later := [p for p in purges if p > cut]:
                raise Unreadable(f"{table}/{partition}: history before {_at(later[-1])} was purged "
                                 "(erasure, or a revoked consent)")
            parts = [p for p in parts if p[4] <= cut]
        snaps = [p for p in parts if p[1] == "snap"]
        start = snaps[-1][0] if snaps else -1
        return [p for p in parts if p[0] >= start]

    def _partitions(self, table: str) -> list[str]:
        cut = len(self._prefix(table))
        return sorted({k[cut:].split("/", 1)[0] for k in self.backend.list_keys(self._prefix(table)) if "/" in k[cut:]})

    # ------------------------------------------------------------ write

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

    def _write(self, table: str, partition: str, kind: str, k: str, df: pl.DataFrame) -> tuple[str, int]:
        """Encrypt and store one part with the partition's next seq. Returns its object key and
        commit ns."""
        parts, _ = self._all(table, partition)
        seq, ns = (parts[-1][0] + 1 if parts else 0), self._commit_ns()
        key = f"{self._prefix(table, partition)}{kind}-{seq:06d}-{ns:020d}.{k}.parquet"
        con, name = self._connection(), self._key_name(partition, k, create=True)
        # A column of nulls with no type cannot cross the Arrow stream; store it as text.
        src = _Stream(df.with_columns(pl.col(pl.Null).cast(pl.String)))
        with tempfile.TemporaryDirectory(prefix="matsya-w-") as d:
            f = Path(d) / "part.parquet"  # already ciphertext when it lands here
            con.register("matsya_src", src)
            try:
                con.execute(f"COPY (SELECT * FROM matsya_src) TO {_sql_str(str(f))} "
                            f"(FORMAT parquet, ENCRYPTION_CONFIG {{footer_key: '{name}'}})")
            finally:
                con.unregister("matsya_src")
            self.backend.put(key, f.read_bytes())
        return key, ns

    def _purge(self, table: str, partition: str, keys: list[str], ns: int) -> None:
        """Delete objects from the partition's history and record that it happened at ns."""
        self.backend.put(f"{self._prefix(table, partition)}purged-{ns:020d}", b"")
        for key in keys:
            self.backend.delete(key)

    def append(self, table: str, df: pl.DataFrame, tenant_id: str | None = None, *,
               platform_key: bool = False) -> str | None:
        df = self._check(table, df, tenant_id)
        if df.is_empty():
            return None
        return self._write(table, self._partition(table, tenant_id), "part", self._key_id(table, platform_key), df)[0]

    def replace(self, table: str, df: pl.DataFrame, tenant_id: str | None = None, *,
                platform_key: bool = False, purge: bool = False) -> None:
        """Swap the partition's rows. What it supersedes stays readable as of earlier times,
        unless purge: then it is deleted, and reads as of earlier times raise."""
        df = self._check(table, df, tenant_id)
        partition = self._partition(table, tenant_id)
        new, ns = self._write(table, partition, "snap", self._key_id(table, platform_key), df)
        if purge:
            self._purge(table, partition, [p[3] for p in self._all(table, partition)[0] if p[3] != new], ns)

    # ------------------------------------------------------------ read

    def files(self, table: str, tenant_id: str | None = None, as_of: datetime | None = None) -> list[str]:
        """Object keys of the parts a read sees: one partition, or every partition of the table."""
        partitions = [self._partition(table, tenant_id)] if tenant_id or not tenant_scoped(table) \
            else self._partitions(table)
        return [p[3] for part in partitions for p in self._live(table, part, as_of)]

    def _scan_sql(self, table: str, tenant_id: str | None = None, as_of: datetime | None = None) -> str | None:
        """SQL reading the table's parts in place, one read_parquet per key, or None if there
        are none. Raises Unreadable when a key is gone or the history was purged."""
        partitions = [self._partition(table, tenant_id)] if tenant_id or not tenant_scoped(table) \
            else self._partitions(table)
        scans = []
        for partition in partitions:
            groups: dict[str, list[str]] = {}
            for _, _, k, key, _ in self._live(table, partition, as_of):
                groups.setdefault(k, []).append(key)
            for k, keys in sorted(groups.items()):
                urls = ", ".join(_sql_str(self.backend.url(x)) for x in keys)
                scans.append(f"SELECT * FROM read_parquet([{urls}], union_by_name = true, "
                             f"encryption_config = {{footer_key: '{self._key_name(partition, k)}'}})")
        return " UNION ALL BY NAME ".join(scans) if scans else None

    def _run(self, what: str, sql: str) -> pl.DataFrame:
        try:
            return pl.DataFrame(self._connection().sql(sql))
        except duckdb.Error as e:
            if re.search(r"AES|encrypt|decrypt|Parquet magic|parquet file", str(e), re.I):
                raise Unreadable(f"{what}: {str(e).splitlines()[0]}") from e
            raise

    def read(self, table: str, tenant_id: str | None = None, as_of: datetime | None = None) -> pl.DataFrame:
        """The table's rows, now or as the store held them at as_of (timezone-aware)."""
        sql = self._scan_sql(table, tenant_id, as_of)
        if sql is None:
            return empty(table) if table in TABLES else pl.DataFrame()
        df = self._run(table, sql)
        if tenant_id and tenant_scoped(table) and df.filter(pl.col("tenant_id") != tenant_id).height:
            raise Unreadable(f"{table}/{tenant_id}: holds rows of another tenant")
        if table in TABLES:
            want = empty(table)
            df = df.select(want.columns).cast(want.schema)
        return df

    def tenants(self, table: str) -> list[str]:
        return [p for p in self._partitions(table) if p != SHARED]

    def tables(self) -> list[str]:
        found = set()
        for key in self.backend.list_keys("tables/"):
            parts = key.split("/")
            if len(parts) == 5 and _PART.match(parts[4]):
                found.add(f"{parts[1]}.{parts[2]}")
        return sorted(found)

    def query(self, sql: str, as_of: datetime | None = None) -> pl.DataFrame:
        """Run SQL over the stored tables, addressed as schema.name (e.g. ledger.transactions),
        now or as of a time. Each table the SQL names becomes a view reading its encrypted
        parts in place."""
        con = self._connection()
        for table in (t for t in self.tables() if re.search(rf"\b{re.escape(t)}\b", sql)):
            scan = self._scan_sql(table, as_of=as_of)
            if scan is None:
                continue
            schema, name = table.split(".", 1)
            con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            con.execute(f'CREATE OR REPLACE VIEW "{schema}"."{name}" AS {scan}')
        return self._run("query", sql)

    # ------------------------------------------------------------ erasure

    def erase_tenant(self, tenant_id: str, keep: tuple[str, ...] = ("ops.events",)) -> dict[str, int]:
        """Remove every stored object of the tenant, history included. In the tables in keep,
        only the parts under the platform key stay (the record of the consent and its
        erasure); the tenant-key parts go. Each partition that lost anything gets a purge
        marker. Returns the objects removed per table. Run after the key is destroyed, every
        removed part was already noise; this removes the noise too."""
        removed, ns = {}, self._commit_ns()
        for table in self.tables():
            gone = [p[3] for p in self._all(table, tenant_id)[0] if table not in keep or p[2] == "t"]
            if gone:
                self._purge(table, tenant_id, gone, ns)
                removed[table] = len(gone)
        return removed
