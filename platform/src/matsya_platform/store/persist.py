"""Persistence over the pure builders: write what build() and match() return, read it back.

build() and match() stay pure; this layer only takes their results and stores them. The
auxiliary outputs with no contract table yet go to aux.* with the run id that produced them.
"""

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime

import polars as pl

from matsya_platform.ledger.build import BuildResult
from matsya_platform.match.engine import MatchResult
from matsya_platform.store.tables import TableStore

BUILD_TABLES = {"txns": "ledger.transactions", "balances": "ledger.balances", "quarantine": "ops.dq_quarantine",
                "events": "ops.events", "coverage": "aux.coverage", "unverified": "aux.unverified"}
MATCH_TABLES = {"matches": "ledger.matches", "outcomes": "labels.invoice_outcomes", "links": "aux.match_links",
                "review": "aux.match_review", "events": "aux.match_events", "label_evidence": "aux.label_evidence"}


def _json(v) -> str:
    def default(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if is_dataclass(o):
            return asdict(o)
        if isinstance(o, (set, tuple)):
            return list(o)
        raise TypeError(type(o))
    return json.dumps(v, default=default, sort_keys=True)


def persist_build(store: TableStore, result: BuildResult, tenant_id: str, run_id: str) -> dict[str, int]:
    written = {}
    for attr, table in BUILD_TABLES.items():
        df = getattr(result, attr)
        if table.startswith("aux."):
            df = df.with_columns(pl.lit(run_id).alias("run_id"))
        store.append(table, df, tenant_id)
        written[table] = df.height
    return written


def persist_match(store: TableStore, result: MatchResult, tenant_id: str, run_id: str) -> dict[str, int]:
    run = pl.lit(run_id).alias("run_id")
    frames = {
        "ledger.matches": result.matches,
        "labels.invoice_outcomes": result.outcomes,
        "aux.match_links": result.links.with_columns(run),
        # One row per review item; the candidate evidence is nested, so it is kept as JSON.
        "aux.match_review": pl.DataFrame(
            [{"txn_uid": x["txn_uid"], "position": x["position"], "cause": x["cause"], "reason": x["reason"],
              "risk": x["risk"], "amount_paise": x["amount_paise"], "item_json": _json(x)} for x in result.review_ranked],
            schema={"txn_uid": pl.String, "position": pl.Int64, "cause": pl.String, "reason": pl.String,
                    "risk": pl.Float64, "amount_paise": pl.Int64, "item_json": pl.String}).with_columns(run),
        "aux.match_events": pl.DataFrame([{"kind": e["kind"], "event_json": _json(e)} for e in result.events],
                                         schema={"kind": pl.String, "event_json": pl.String}).with_columns(run),
        "aux.label_evidence": pl.DataFrame([{"invoice_id": k, "evidence": v} for k, v in sorted(result.label_evidence.items())],
                                           schema={"invoice_id": pl.String, "evidence": pl.String}).with_columns(run),
    }
    for table, df in frames.items():
        store.append(table, df, tenant_id)
    return {t: df.height for t, df in frames.items()}


def persist_invoices(store: TableStore, invoices: pl.DataFrame, tenant_id: str) -> int:
    """New ledger.invoices rows from ingest."""
    store.append("ledger.invoices", invoices, tenant_id)
    return invoices.height


def persist_refs(store: TableStore, buyers: pl.DataFrame, tenant_buyers: pl.DataFrame | None,
                 tenant_id: str) -> None:
    """The reference tables Track A holds. ref.buyers is shared; ref.tenant_buyers per tenant."""
    store.replace("ref.buyers", buyers)
    if tenant_buyers is not None:
        store.replace("ref.tenant_buyers", tenant_buyers.filter(pl.col("tenant_id") == tenant_id), tenant_id)


def latest_coverage(coverage: pl.DataFrame) -> pl.DataFrame:
    """aux.coverage holds one snapshot per build; the current one is the last build's, by run_id."""
    if coverage.is_empty():
        return coverage
    last = coverage["run_id"].max()
    return coverage.filter(pl.col("run_id") == last).drop("run_id")


def read_build(store: TableStore, tenant_id: str) -> dict[str, pl.DataFrame]:
    return {attr: store.read(table, tenant_id) for attr, table in BUILD_TABLES.items()}


def read_match(store: TableStore, tenant_id: str) -> dict[str, pl.DataFrame]:
    return {attr: store.read(table, tenant_id) for attr, table in MATCH_TABLES.items()}
