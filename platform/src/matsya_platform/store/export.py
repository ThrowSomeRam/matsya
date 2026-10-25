"""Export the table store to the flat layout Track B's contract source reads: one
<namespace>.<table>.parquet per table in one directory, every tenant together.

Every table Track A writes is exported, empty ones included, because the reader opens a file
for each table it reads and a missing file is an error, not an empty table. Each frame is
validated against the contract, on every row, before it is written.

An export is a read of the store as of one time, recorded with a sha256 per file in
_export.json next to the tables. export_contract_dir(store, dir, as_of=<that time>) later
writes the same tables again (the store keeps history), so a forecast made from an export can
be rerun on its exact inputs as long as it records the export's store_as_of. The one
exception is a purge in between (an erasure or a revoked consent): then the re-export raises
Unreadable instead of producing different inputs.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from matsya_contracts.tables import TABLES
from matsya_contracts.validate import validate
from matsya_platform.store.tables import TableStore

EXPORTED = tuple(t for t, m in TABLES.items() if m.writer == "A")
MANIFEST = "_export.json"


def export_contract_dir(store: TableStore, out_dir: str | Path, tables: tuple[str, ...] = EXPORTED,
                        as_of: datetime | None = None) -> dict[str, int]:
    """Write every table as the store held it at as_of (default: now). Returns rows per table."""
    as_of = as_of or datetime.now(timezone.utc)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written, files = {}, {}
    for table in tables:
        df = validate(store.read(table, as_of=as_of), table, sample=None)
        p = out / f"{table}.parquet"
        tmp = p.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(p)
        written[table] = df.height
        files[table] = {"rows": df.height, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    (out / MANIFEST).write_text(json.dumps({"store_as_of": as_of.isoformat(), "tables": files}, indent=1,
                                           sort_keys=True) + "\n")
    return written
