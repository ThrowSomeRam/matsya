"""Load a world folder written by Track B's synth.export.write_contract_parquet.

Layout: <ns>.<table>.parquet for contract tables, truth/<name>.parquet for generator truth.
Every contract table is validated in full against contracts v0.1 before anything renders;
any problem raises WorldError.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from matsya_contracts import ids
from matsya_contracts.tables import TABLES
from matsya_contracts.validate import ContractError, validate
from matsya_platform.ingest.invoices import fiscal_year

TRUTH_TXN_COLUMNS = ("txn_uid", "bank_txn_id", "day_seq", "rail")
_GSTIN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")


class WorldError(ValueError):
    pass


@dataclass(frozen=True)
class World:
    tables: dict[str, pl.DataFrame]  # contract tables, validated
    truth_txns: pl.DataFrame  # truth/transactions (whole world)
    truth_balances: pl.DataFrame
    truth_payments: pl.DataFrame | None
    truth_buyers: pl.DataFrame | None
    truth_invoices: pl.DataFrame | None
    cut: datetime  # the world's as_of: latest known_at in any contract table
    cut_inferred: bool = True  # the export does not record as_of yet

    def supplier_gstin(self) -> str:
        """The supplier's own GSTIN. Not exported yet, so it is read from the GST payment
        narrations and accepted only if it reproduces every exported invoice_id."""
        if self.truth_invoices is None:
            raise WorldError("truth/invoices.parquet is needed to verify the supplier GSTIN")
        gst = self.truth_txns.filter(pl.col("category") == "gst_payment")["narration_raw"]
        found = sorted({m for n in gst for m in _GSTIN.findall(n or "")})
        tenant = self.tables["ledger.transactions"]["tenant_id"][0]
        inv = self.truth_invoices.select("invoice_id", "invoice_no", "issue_date")
        for g in found:
            if all(ids.invoice_id(tenant, g, no, fiscal_year(d)) == i for i, no, d in inv.iter_rows()):
                return g
        raise WorldError(f"no GSTIN in the GST narrations reproduces the invoice ids (tried {found})")

    def rows(self) -> pl.DataFrame:
        """ledger.transactions known at the cut, with the truth columns renderers need."""
        txns = self.tables["ledger.transactions"]
        extra = self.truth_txns.select(*TRUTH_TXN_COLUMNS)
        out = txns.join(extra, on="txn_uid", how="left")
        if out["bank_txn_id"].null_count():
            raise WorldError("ledger.transactions rows missing from truth/transactions")
        return out

    def opening(self) -> dict[str, int]:
        """Per account, the closing on the day before the world starts."""
        first = self.truth_balances.sort("date").group_by("account_id", maintain_order=True).first()
        return dict(zip(first["account_id"], first["closing_paise"]))


def load(folder: str | Path) -> World:
    folder = Path(folder)
    tables = {}
    for p in sorted(folder.glob("*.parquet")):
        name = p.stem
        if name not in TABLES:
            raise WorldError(f"{p.name}: not a contract table")
        df = pl.read_parquet(p)
        try:
            validate(df, name)
        except ContractError as e:
            raise WorldError(f"{p.name} does not validate: {e}") from e
        tables[name] = df
    if "ledger.transactions" not in tables:
        raise WorldError(f"{folder}: no ledger.transactions.parquet")
    truth = folder / "truth"
    need = [truth / "transactions.parquet", truth / "balances.parquet"]
    for p in need:
        if not p.exists():
            raise WorldError(f"missing {p}")
    truth_txns = pl.read_parquet(need[0])
    missing = set(TRUTH_TXN_COLUMNS) - set(truth_txns.columns)
    if missing:
        raise WorldError(f"truth/transactions lacks {sorted(missing)}")
    _check_txn_uids(truth_txns)
    opt = {n: (pl.read_parquet(truth / f"{n}.parquet") if (truth / f"{n}.parquet").exists() else None)
           for n in ("payments", "buyers", "invoices")}
    cut = max(df["known_at"].max() for df in tables.values() if "known_at" in df.columns and df.height)
    return World(tables=tables, truth_txns=truth_txns, truth_balances=pl.read_parquet(need[1]),
                 truth_payments=opt["payments"], truth_buyers=opt["buyers"], truth_invoices=opt["invoices"], cut=cut)


def _check_txn_uids(truth_txns: pl.DataFrame) -> None:
    """Every truth txn_uid must be the txnId-path hash of its bank_txn_id, or expected
    txn_uids recomputed by the renderer would not line up."""
    for r in truth_txns.select("tenant_id", "account_id", "txn_uid", "bank_txn_id", "value_date",
                               "amount_paise").iter_rows(named=True):
        want = ids.txn_uid(r["tenant_id"], r["account_id"],
                           ids.txn_key_bank_id(r["bank_txn_id"], r["value_date"], r["amount_paise"]))
        if want != r["txn_uid"]:
            raise WorldError(f"truth txn_uid {r['txn_uid']} is not the txnId-path hash of {r['bank_txn_id']}")
