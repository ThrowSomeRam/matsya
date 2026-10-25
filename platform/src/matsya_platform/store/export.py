"""Export the table store to the flat layout Track B's contract source reads: one
<namespace>.<table>.parquet per table in one directory, every tenant together.

Every table Track A writes is exported, empty ones included, because the reader opens a file
for each table it reads and a missing file is an error, not an empty table. Each frame is
validated against the contract, on every row, before it is written.
"""

from pathlib import Path

from matsya_contracts.tables import TABLES
from matsya_contracts.validate import validate
from matsya_platform.store.tables import TableStore

EXPORTED = tuple(t for t, m in TABLES.items() if m.writer == "A")


def export_contract_dir(store: TableStore, out_dir: str | Path, tables: tuple[str, ...] = EXPORTED) -> dict[str, int]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for table in tables:
        df = validate(store.read(table), table, sample=None)
        p = out / f"{table}.parquet"
        tmp = p.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(p)
        written[table] = df.height
    return written
