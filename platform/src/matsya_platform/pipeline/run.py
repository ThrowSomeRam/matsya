"""The Track A pipeline as one repeatable run: every component, in order, for every tenant.

A run, at a time `now` (the clock, or a simulated one):
1. Consents: every consent past its expiry expires (and its erasure runs).
2. A2 fetch: under the tenant's active consent, whatever the AA source holds up to now goes
   through the fetch runner into the encrypted raw store; a payload already stored (same
   sha256) is skipped there.
3. A4 build: the ledger builder's working state is rebuilt by replaying the payloads already
   applied, from the raw store, in the order they were applied (build() is pure, and the
   state cannot be rebuilt from ledger.transactions; positions doc F2). Then each payload
   not yet built is built, persisted, and logged in aux.build_log. A payload whose balances
   do not continue is logged as failed and left out of the state; it is not retried, because
   only the owner can accept a gap (ops.owner_inputs, not built). A tenant with no active
   consent is skipped from here on, invoices included.
4. Reference tables: ref.buyers and ref.tenant_buyers from ref_dir, replaced only when their
   content changed (the store keeps every version).
5. A6 invoice ingest: each new file in the inbox (one folder per adapter, by name order) goes
   through its adapter; aux.ingest_log keeps its sha256, so a file is ingested once.
6. A6 matcher: over everything stored, as of now and complete through the tenant's coverage,
   with Track B's features.txn_category for the category gate when the config names it.
   Only match rows and labels that differ from the latest stored version are appended, so an
   unchanged run writes nothing.
7. Export for Track B, once after all tenants, as of one time (store/export.py).

The run is idempotent: run twice at the same now and the second writes nothing (it still
rewrites the export). One run at a time per store: a lock file under the store root makes a
second, overlapping run exit instead of interleaving writes (the store's history assumes one
writer). A tenant that fails is reported and the others still run.
"""

import fcntl
import json
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import polars as pl

from matsya_contracts.enums import ConsentStatus as S
from matsya_contracts.validate import empty
from matsya_platform.consent.service import ConsentService
from matsya_platform.fetch.adapter import LocalFolderAdapter
from matsya_platform.fetch.runner import fetch, stored_payloads
from matsya_platform.ingest.invoices import BuyerDirectory, ingest
from matsya_platform.ingest.registry import ADAPTERS, adapter
from matsya_platform.ledger.build import LedgerState, build, validate_outputs
from matsya_platform.ledger.continuity import ContinuityError
from matsya_platform.match.engine import match
from matsya_platform.pipeline.config import PipelineConfig, TenantConfig
from matsya_platform.store.export import export_contract_dir
from matsya_platform.store.keys import KeyStore
from matsya_platform.store.objects import open_objects
from matsya_platform.store.persist import latest_coverage, persist_build, persist_invoices, persist_match
from matsya_platform.store.raw import RawStore
from matsya_platform.store.tables import TableStore

UTC = timezone.utc


class RunInProgress(RuntimeError):
    pass


@dataclass
class TenantReport:
    tenant_id: str
    consent: str | None = None
    fetched: int = 0
    duplicates: int = 0
    built: int = 0
    failed: list[dict] = field(default_factory=list)
    replayed: int = 0
    replay_missing: int = 0
    refs_replaced: list[str] = field(default_factory=list)
    invoice_files: int = 0
    invoices: int = 0
    invoice_rejects: int = 0
    matched: dict | None = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        m = self.matched or {}
        return bool(self.fetched or self.built or self.failed or self.refs_replaced or self.invoice_files
                    or m.get("new_matches") or m.get("new_labels") or self.error)


@dataclass
class RunReport:
    run_id: str
    now: str
    expired: list[str]
    tenants: list[TenantReport]
    export: dict[str, int] | None = None

    @property
    def ok(self) -> bool:
        return all(t.error is None for t in self.tenants)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1, default=str)


def services(store: Path) -> ConsentService:
    keys, objects = KeyStore(store), open_objects(store)
    return ConsentService(TableStore(store, keys, objects), keys, RawStore(store, keys, objects))


@contextmanager
def lock(store: Path):
    store.mkdir(parents=True, exist_ok=True)
    with open(store / ".pipeline.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunInProgress(f"another pipeline run holds {store / '.pipeline.lock'}") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def active_consent(svc: ConsentService, tenant_id: str) -> str | None:
    c = svc.consents(tenant_id).filter(pl.col("status") == S.ACTIVE.value)
    return c["consent_id"].sort()[0] if c.height else None


# ---------------------------------------------------------------- stages


def _fetch(svc: ConsentService, t: TenantConfig, consent_id: str, now: datetime, rep: TenantReport) -> None:
    commit = fetch(svc, LocalFolderAdapter(t.aa_folder, until=now), t.tenant_id, consent_id, now=lambda: now).commit
    rep.fetched, rep.duplicates = len(commit.stored), len(commit.duplicates)


def _build(svc: ConsentService, t: TenantConfig, run_id: str, rep: TenantReport) -> None:
    tables, tenant = svc.tables, t.tenant_id
    log = tables.read("aux.build_log", tenant)
    payloads = {p.payload_id: p for p in stored_payloads(svc, tenant)}
    state, seq = LedgerState(), 0
    if log.height:
        seq = int(log["seq"].max()) + 1
        for pid in log.filter(pl.col("outcome") == "applied").sort("seq")["payload_id"]:
            if pid not in payloads:  # its consent was revoked while another stayed live
                rep.replay_missing += 1
                continue
            state = build([payloads[pid]], state).state
            rep.replayed += 1
    done = set(log["payload_id"]) if log.height else set()
    rows = []
    for p in sorted((p for pid, p in payloads.items() if pid not in done), key=lambda p: (p.fetched_at, p.payload_id)):
        try:
            r = build([p], state)
        except ContinuityError as e:
            breaks = [{"account_id": b.account_id, "on": b.on.isoformat(), "gap_paise": b.gap_paise} for b in e.breaks]
            rows.append({"payload_id": p.payload_id, "fetched_at": p.fetched_at, "outcome": "failed_continuity",
                         "seq": seq, "run_id": run_id, "detail_json": json.dumps(breaks)})
            rep.failed.append({"payload_id": p.payload_id, "breaks": breaks})
            seq += 1
            continue
        validate_outputs(r, sample=None)
        persist_build(tables, r, tenant, f"{run_id}:{p.payload_id}")
        rows.append({"payload_id": p.payload_id, "fetched_at": p.fetched_at, "outcome": "applied", "seq": seq,
                     "run_id": run_id, "detail_json": json.dumps({"txns": r.txns.height,
                                                                  "quarantined": r.quarantine.height})})
        state, seq = r.state, seq + 1
        rep.built += 1
    if rows:
        tables.append("aux.build_log", pl.DataFrame(rows, schema={
            "payload_id": pl.String, "fetched_at": pl.Datetime("us", "UTC"), "outcome": pl.String, "seq": pl.Int64,
            "run_id": pl.String, "detail_json": pl.String}), tenant)


def _as_contract(df: pl.DataFrame, table: str) -> pl.DataFrame:
    want = empty(table)
    return df.select(want.columns).cast(want.schema)


def _same(a: pl.DataFrame, b: pl.DataFrame) -> bool:
    return a.height == b.height and a.sort(a.columns).equals(b.select(a.columns).sort(a.columns))


def _refs(svc: ConsentService, cfg: PipelineConfig, t: TenantConfig, rep: TenantReport) -> None:
    if cfg.ref_dir is None:
        return
    tables = svc.tables
    buyers_file, tb_file = cfg.ref_dir / "ref.buyers.parquet", cfg.ref_dir / "ref.tenant_buyers.parquet"
    if buyers_file.exists():
        buyers = _as_contract(pl.read_parquet(buyers_file), "ref.buyers")
        if not _same(buyers, tables.read("ref.buyers")):
            tables.replace("ref.buyers", buyers)
            rep.refs_replaced.append("ref.buyers")
    if tb_file.exists():
        mine = _as_contract(pl.read_parquet(tb_file), "ref.tenant_buyers").filter(pl.col("tenant_id") == t.tenant_id)
        if not _same(mine, tables.read("ref.tenant_buyers", t.tenant_id)):
            tables.replace("ref.tenant_buyers", mine, t.tenant_id)
            rep.refs_replaced.append("ref.tenant_buyers")


def _invoices(svc: ConsentService, t: TenantConfig, now: datetime, run_id: str, rep: TenantReport) -> None:
    if t.invoice_inbox is None or not t.invoice_inbox.exists():
        return
    tables, tenant = svc.tables, t.tenant_id
    log = tables.read("aux.ingest_log", tenant)
    seen_files = set(log["sha256"]) if log.height else set()
    known = frozenset(tables.read("ledger.invoices", tenant)["invoice_id"])
    buyers = BuyerDirectory(tables.read("ref.buyers"), tables.read("ref.tenant_buyers", tenant), tenant)
    rows = []
    for name in sorted(ADAPTERS):
        folder = t.invoice_inbox / name
        for f in sorted(folder.iterdir()) if folder.is_dir() else []:
            if not f.is_file() or f.name.startswith("."):
                continue
            data = f.read_bytes()
            digest = sha256(data).hexdigest()
            if digest in seen_files:
                continue
            r = ingest(data, adapter(name), tenant_id=tenant, supplier_gstin=t.supplier_gstin, received_at=now,
                       buyers=buyers, existing_invoice_ids=known)
            persist_invoices(tables, r.invoices, tenant)
            known |= frozenset(r.invoices["invoice_id"])
            seen_files.add(digest)
            rows.append({"file": str(f.relative_to(t.invoice_inbox)), "sha256": digest, "adapter": name,
                         "run_id": run_id, "invoices": r.invoices.height, "duplicates": len(r.duplicates),
                         "rejects_json": json.dumps([asdict(x) for x in r.rejects], default=str)})
            rep.invoice_files += 1
            rep.invoices += r.invoices.height
            rep.invoice_rejects += len(r.rejects)
    if rows:
        tables.append("aux.ingest_log", pl.DataFrame(rows, schema={
            "file": pl.String, "sha256": pl.String, "adapter": pl.String, "run_id": pl.String, "invoices": pl.Int64,
            "duplicates": pl.Int64, "rejects_json": pl.String}), tenant)


def _new_versions(new: pl.DataFrame, stored: pl.DataFrame, key: list[str]) -> pl.DataFrame:
    """The rows of new that are not already the latest stored version of their key."""
    if stored.is_empty() or new.is_empty():
        return new
    latest = stored.sort("known_at").group_by(key, maintain_order=True).last()
    facts = [c for c in new.columns if c != "known_at"]
    return new.join(latest.select(facts), on=facts, how="anti", nulls_equal=True)


def _match(svc: ConsentService, t: TenantConfig, now: datetime, run_id: str, rep: TenantReport) -> None:
    tables, tenant = svc.tables, t.tenant_id
    cov = latest_coverage(tables.read("aux.coverage", tenant))
    invoices = tables.read("ledger.invoices", tenant)
    if cov.is_empty() or invoices.is_empty():
        rep.matched = {"skipped": "no coverage yet" if cov.is_empty() else "no invoices yet"}
        return
    cats = pl.read_parquet(t.txn_category) if t.txn_category and t.txn_category.exists() else None
    r = match(tables.read("ledger.transactions", tenant), invoices, tables.read("ref.buyers"),
              tables.read("ref.tenant_buyers", tenant), tenant_id=tenant, supplier_gstin=t.supplier_gstin,
              as_of=now, complete_through=cov["complete_through"].min(), txn_category=cats)
    matches = _new_versions(r.matches, tables.read("ledger.matches", tenant), ["txn_uid", "invoice_id"])
    labels = _new_versions(r.outcomes, tables.read("labels.invoice_outcomes", tenant), ["invoice_id"])
    rep.matched = {"links": r.matches.height, "labels": r.outcomes.height, "review": len(r.review_ranked),
                   "category_gate": cats is not None, "new_matches": matches.height, "new_labels": labels.height}
    if matches.height or labels.height:
        persist_match(tables, replace(r, matches=matches, outcomes=labels), tenant, run_id)


# ---------------------------------------------------------------- run


def run_tenant(svc: ConsentService, cfg: PipelineConfig, t: TenantConfig, now: datetime, run_id: str) -> TenantReport:
    rep = TenantReport(t.tenant_id)
    try:
        # Without an active consent nothing runs for the tenant, invoices included: after an
        # erasure, ingesting the inbox again would bring the erased tenant's data back.
        rep.consent = active_consent(svc, t.tenant_id)
        if rep.consent is not None:
            _fetch(svc, t, rep.consent, now, rep)
            _build(svc, t, run_id, rep)
            _refs(svc, cfg, t, rep)
            _invoices(svc, t, now, run_id, rep)
            _match(svc, t, now, run_id, rep)
    except Exception as e:  # one tenant's failure must not stop the others
        rep.error = f"{type(e).__name__}: {e}"
        rep.matched = rep.matched or {}
        rep.matched["traceback"] = traceback.format_exc(limit=5)
    if rep.changed and rep.consent is not None:
        svc.tables.append("aux.pipeline_runs", pl.DataFrame(
            [{"run_id": run_id, "now": now, "report_json": json.dumps(asdict(rep), default=str)}],
            schema={"run_id": pl.String, "now": pl.Datetime("us", "UTC"), "report_json": pl.String}), t.tenant_id)
    return rep


def run(cfg: PipelineConfig, now: datetime | None = None, *, export: bool = True) -> RunReport:
    now = now or datetime.now(UTC)
    run_id = f"run-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    with lock(cfg.store):
        svc = services(cfg.store)
        expired = svc.expire_due(now)
        report = RunReport(run_id, now.isoformat(), expired, [run_tenant(svc, cfg, t, now, run_id) for t in cfg.tenants])
        if export:
            report.export = export_contract_dir(svc.tables, cfg.export_dir)
    return report


# ---------------------------------------------------------------- consent (stands in for the AA consent flow)


def grant(cfg: PipelineConfig, tenant_id: str, now: datetime | None = None) -> str:
    """Create the tenant's consent from its spec and activate it, as the AA would on the
    owner's approval. There is no AA consent flow (A1 notifications are not built)."""
    now = now or datetime.now(UTC)
    t = cfg.tenant(tenant_id)
    c = t.consent
    with lock(cfg.store):
        svc = services(cfg.store)
        svc.create(tenant_id=tenant_id, consent_id=c.consent_id, fip_id=c.fip_id, purpose=c.purpose,
                   range_from=c.range_from or (now - timedelta(days=365)).date(), range_to=c.range_to or now.date(),
                   frequency=c.frequency, expires_at=now + timedelta(days=c.valid_days), at=now)
        svc.transition(tenant_id, c.consent_id, S.ACTIVE, now)
    return c.consent_id


def set_status(cfg: PipelineConfig, tenant_id: str, consent_id: str, status: S, now: datetime | None = None) -> str:
    """Pause, resume or revoke; a revoke runs the erasure."""
    now = now or datetime.now(UTC)
    with lock(cfg.store):
        return services(cfg.store).transition(tenant_id, consent_id, status, now).status.value


def status(cfg: PipelineConfig) -> list[dict]:
    svc = services(cfg.store)
    out = []
    for t in cfg.tenants:
        consents = svc.consents(t.tenant_id)
        runs = svc.tables.read("aux.pipeline_runs", t.tenant_id)
        log = svc.tables.read("aux.build_log", t.tenant_id)
        out.append({"tenant_id": t.tenant_id,
                    "consents": consents.select("consent_id", "status", "expires_at").to_dicts(),
                    "payloads_applied": int((log["outcome"] == "applied").sum()) if log.height else 0,
                    "payloads_failed": int((log["outcome"] != "applied").sum()) if log.height else 0,
                    "last_run": runs.sort("now")["now"][-1] if runs.height else None})
    return out
