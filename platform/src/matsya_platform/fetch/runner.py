"""A2 fetch runner: consent gate, retries, idempotent commit, and read-back for build()."""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


from matsya_platform.consent.service import CommitResult, ConsentService
from matsya_platform.fetch.adapter import AaAdapter, TransientFetchError
from matsya_platform.ledger.rebit import Payload


class FetchGaveUp(RuntimeError):
    pass


@dataclass
class FetchReport:
    attempts: int = 0
    failures: list[str] = field(default_factory=list)
    commit: CommitResult | None = None


def fetch(service: ConsentService, adapter: AaAdapter, tenant_id: str, consent_id: str, *, retries: int = 3,
          backoff_s: float = 1.0, sleep: Callable[[float], None] = time.sleep,
          now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> FetchReport:
    """One fetch under a consent. Transient failures are retried up to retries times with
    doubling backoff, each recorded as a fetch_failed event. The payloads are committed
    through the consent gate, which discards them if the consent left active meanwhile."""
    ticket = service.begin_fetch(tenant_id, consent_id)
    consent = service.get(tenant_id, consent_id)
    report = FetchReport()
    for attempt in range(1, retries + 1):
        report.attempts = attempt
        try:
            payloads = adapter.fetch(consent, attempt)
        except TransientFetchError as e:
            report.failures.append(str(e))
            service.fetch_failed(ticket, consent.fip_id, attempt, str(e), now())
            if attempt < retries:
                sleep(backoff_s * 2 ** (attempt - 1))
            continue
        report.commit = service.commit_fetch(ticket, payloads)
        return report
    raise FetchGaveUp(f"consent {consent_id}: {retries} attempts failed: {report.failures[-1]}")


def stored_payloads(service: ConsentService, tenant_id: str) -> list[Payload]:
    """Every stored payload of the tenant, decrypted and parsed, in fetch order, for build()."""
    rows = service.tables.read("raw.aa_payloads", tenant_id).sort("fetched_at", "payload_id")
    return [Payload(tenant_id=tenant_id, payload_id=r["payload_id"], fetched_at=r["fetched_at"],
                    doc=json.loads(service.raw.get(tenant_id, r["payload_id"])))
            for r in rows.iter_rows(named=True)]
