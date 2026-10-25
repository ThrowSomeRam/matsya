"""Pipeline configuration: one TOML file naming the store, the export and each tenant's sources.

    store = "/var/lib/matsya/store"          # keys, and the objects unless MATSYA_S3_ENDPOINT is set
    export_dir = "/var/lib/matsya/contract"  # what Track B reads
    ref_dir = "/var/lib/matsya/ref"          # ref.buyers.parquet [, ref.tenant_buyers.parquet]

    [[tenant]]
    tenant_id = "5f0c6a1e-..."
    supplier_gstin = "13AJTCC2621Z1Z9"
    aa_folder = "/var/lib/matsya/aa/5f0c6a1e"   # stub AA source: manifest.json + payloads/
    invoice_inbox = "/var/lib/matsya/inbox/5f0c6a1e"  # <adapter name>/<files>
    txn_category = "/var/lib/matsya/features/features.txn_category.parquet"  # optional, Track B
    [tenant.consent]                            # what `consent grant` asks the AA for
    consent_id = "C1"
    fip_id = "FIP-STUB"
    range_from = 2025-04-01
    range_to = 2026-03-31
    frequency = "weekly"
    valid_days = 365

Relative paths are relative to the config file. The invoice inbox has one folder per adapter
(csv_generic, tally_daybook, gst_einvoice; ingest/registry.py), because a file's format is
what its uploader says it is, not something to guess.
"""

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class ConsentSpec:
    consent_id: str = "C1"
    fip_id: str = "FIP-STUB"
    purpose: str = "cash-flow forecast"
    range_from: date | None = None
    range_to: date | None = None
    frequency: str = "weekly"
    valid_days: int = 365


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    supplier_gstin: str
    aa_folder: Path
    invoice_inbox: Path | None = None
    txn_category: Path | None = None
    consent: ConsentSpec = field(default_factory=ConsentSpec)


@dataclass(frozen=True)
class PipelineConfig:
    store: Path
    export_dir: Path
    ref_dir: Path | None
    tenants: tuple[TenantConfig, ...]

    def tenant(self, tenant_id: str) -> TenantConfig:
        for t in self.tenants:
            if t.tenant_id == tenant_id:
                return t
        raise KeyError(f"tenant {tenant_id} is not in the config")


def load(path: str | Path) -> PipelineConfig:
    path = Path(path)
    raw = tomllib.loads(path.read_text())
    base = path.parent

    def p(v):
        return None if v is None else (base / v).resolve()

    tenants = []
    for t in raw.get("tenant", []):
        tenants.append(TenantConfig(tenant_id=t["tenant_id"], supplier_gstin=t["supplier_gstin"],
                                    aa_folder=p(t["aa_folder"]), invoice_inbox=p(t.get("invoice_inbox")),
                                    txn_category=p(t.get("txn_category")),
                                    consent=ConsentSpec(**t.get("consent", {}))))
    if len({t.tenant_id for t in tenants}) != len(tenants):
        raise ValueError(f"{path}: a tenant is listed twice")
    return PipelineConfig(store=p(raw["store"]), export_dir=p(raw["export_dir"]), ref_dir=p(raw.get("ref_dir")),
                          tenants=tuple(tenants))
