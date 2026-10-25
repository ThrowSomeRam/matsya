"""A2 adapters. There is no AA sandbox: LocalFolderAdapter is a stub over a folder of rendered
payloads, not an AA client, and nothing here talks to a network.

A real AA client is a swap of this one class. It implements AaAdapter.fetch(): given the
consent, return the payloads the FIP delivered as original bytes (after the AA's own
decryption), and raise TransientFetchError for anything worth retrying (timeouts, FIP not
ready, 5xx); any other exception is permanent and stops the run. The runner, consent gate,
idempotency and raw store do not change.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from matsya_contracts.tables import Consent
from matsya_platform.consent.service import Fetched


class TransientFetchError(RuntimeError):
    pass


class AaAdapter(Protocol):
    def fetch(self, consent: Consent, attempt: int) -> list[Fetched]: ...


class LocalFolderAdapter:
    """Reads a rendered folder (manifest.json deliveries plus payloads/*.json). Each payload
    is returned byte for byte as rendered, so its sha256 is stable across re-reads, which is
    what makes a repeated fetch idempotent. fail_attempts makes the first N attempts raise
    TransientFetchError, to exercise retries; until is an optional fetched_at cut; only, when
    given, limits a fetch to those payload ids (one delivery at a time, as the eval loop runs)."""

    def __init__(self, folder: str | Path, fail_attempts: int = 0, until: datetime | None = None,
                 only: set[str] | None = None):
        self.folder = Path(folder)
        self.fail_attempts = fail_attempts
        self.until = until
        self.only = only

    def fetch(self, consent: Consent, attempt: int) -> list[Fetched]:
        if attempt <= self.fail_attempts:
            raise TransientFetchError(f"stub: attempt {attempt} fails by configuration")
        manifest = json.loads((self.folder / "manifest.json").read_text())
        out = []
        for d in manifest["deliveries"]:
            at = datetime.fromisoformat(d["fetched_at"])
            if d["tenant_id"] != consent.tenant_id or (self.until is not None and at > self.until) \
                    or (self.only is not None and d["payload_id"] not in self.only):
                continue
            out.append(Fetched(payload_id=d["payload_id"], fetched_at=at, data=(self.folder / d["file"]).read_bytes()))
        return out
