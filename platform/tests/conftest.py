import json
from datetime import datetime
from pathlib import Path

from matsya_platform.ledger.rebit import Payload

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = "3b2f7c1e-9a4d-4e8b-b6c5-1d2e3f4a5b6c"
ACCOUNT = "LINK-ACC-1"


def payload(name: str, fetched_at: str, payload_id: str | None = None) -> Payload:
    doc = json.loads((FIXTURES / f"{name}.json").read_text())
    return Payload(tenant_id=TENANT, payload_id=payload_id or f"PL-{name}", fetched_at=datetime.fromisoformat(fetched_at),
                   doc=doc)
