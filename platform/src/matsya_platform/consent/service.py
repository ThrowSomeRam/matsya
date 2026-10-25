"""A1 consent service, deliberately small.

States are the contract's ConsentStatus. Allowed transitions:

    pending -> active | revoked | expired
    active  -> paused | revoked | expired
    paused  -> active | revoked | expired
    revoked, expired: terminal, nothing leaves them

Every change rewrites the tenant's ops.consents partition and appends a consent_changed
event. Entering a terminal state triggers erasure:
- the consent's raw objects and their raw.aa_payloads rows are removed;
- when no other consent of the tenant is still live, the tenant's key is destroyed (every
  raw object it ever wrote becomes unreadable, wherever a copy sits) and the tenant's
  derived rows are removed from the table store. ops.consents and ops.events are kept: they
  hold the record that consent existed and was ended, not financial data;
- an erasure_completed event records how many keys were destroyed.

In-flight fetches: begin_fetch() hands out a ticket carrying the consent's generation, which
every transition bumps. commit_fetch() takes the same lock as transition(), so a revoke
either lands before the commit (the payload is discarded, never stored) or after it (the
payload is stored, then erased with the rest). A payload whose consent is no longer active at
commit, revoked, paused or expired, is discarded.
"""

import hashlib
import threading
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from matsya_contracts import events as E
from matsya_contracts.enums import ConsentStatus as S, EventKind
from matsya_contracts.ids import hash32
from matsya_contracts.tables import AaPayload, Consent, Event
from matsya_contracts.validate import frame
from matsya_platform.store.keys import KeyStore
from matsya_platform.store.raw import RawStore
from matsya_platform.store.tables import TableStore

TRANSITIONS: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.ACTIVE, S.REVOKED, S.EXPIRED}),
    S.ACTIVE: frozenset({S.PAUSED, S.REVOKED, S.EXPIRED}),
    S.PAUSED: frozenset({S.ACTIVE, S.REVOKED, S.EXPIRED}),
    S.REVOKED: frozenset(),
    S.EXPIRED: frozenset(),
}
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class TransitionError(ValueError):
    pass


@dataclass(frozen=True)
class FetchTicket:
    tenant_id: str
    consent_id: str
    generation: int


@dataclass(frozen=True)
class Fetched:
    """One payload as an adapter returned it: original bytes, not yet stored."""

    payload_id: str
    fetched_at: datetime
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class CommitResult:
    stored: tuple[str, ...]  # payload ids written
    duplicates: tuple[str, ...]  # already stored (same payload hash or id): skipped
    discarded: tuple[str, ...]  # consent no longer active at commit: never stored


class ConsentService:
    def __init__(self, tables: TableStore, keys: KeyStore, raw: RawStore):
        self.tables, self.keys, self.raw = tables, keys, raw
        self._lock = threading.RLock()
        self._generation: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- state

    def consents(self, tenant_id: str) -> pl.DataFrame:
        return self.tables.read("ops.consents", tenant_id)

    def get(self, tenant_id: str, consent_id: str) -> Consent:
        rows = self.consents(tenant_id).filter(pl.col("consent_id") == consent_id)
        if rows.is_empty():
            raise KeyError(f"no consent {consent_id} for tenant {tenant_id}")
        return Consent.model_validate(rows.row(0, named=True))

    def _save(self, c: Consent) -> None:
        others = [Consent.model_validate(r) for r in self.consents(c.tenant_id).iter_rows(named=True)
                  if r["consent_id"] != c.consent_id]
        self.tables.replace("ops.consents", frame(Consent, sorted(others + [c], key=lambda x: x.consent_id)),
                            c.tenant_id)

    def _event(self, tenant_id: str, kind: EventKind, payload: E.Payload, at: datetime) -> None:
        body = payload.model_dump_json()
        ev = Event(tenant_id=tenant_id, event_id=hash32(tenant_id, kind.value, body, at.isoformat()), kind=kind,
                   payload_json=body, at=at)
        self.tables.append("ops.events", frame(Event, [ev]), tenant_id)

    def create(self, *, tenant_id: str, consent_id: str, fip_id: str, purpose: str, range_from: date,
               range_to: date, frequency: str, expires_at: datetime, at: datetime) -> Consent:
        with self._lock:
            if not self.consents(tenant_id).filter(pl.col("consent_id") == consent_id).is_empty():
                raise TransitionError(f"consent {consent_id} already exists")
            c = Consent(tenant_id=tenant_id, consent_id=consent_id, fip_id=fip_id, purpose=purpose,
                        range_from=range_from, range_to=range_to, frequency=frequency, status=S.PENDING,
                        expires_at=expires_at)
            self._save(c)
            self._event(tenant_id, EventKind.CONSENT_CHANGED,
                        E.ConsentChanged(consent_id=consent_id, status=S.PENDING, previous=None), at)
            return c

    def transition(self, tenant_id: str, consent_id: str, to: S, at: datetime) -> Consent:
        with self._lock:
            c = self.get(tenant_id, consent_id)
            if c.status not in TERMINAL and to != S.EXPIRED and at >= c.expires_at:
                c = self._move(c, S.EXPIRED, at)  # past expiry: it expired first, whatever was asked
                raise TransitionError(f"consent {consent_id} expired at {c.expires_at}; cannot move to {to}")
            return self._move(c, S(to), at)

    def _move(self, c: Consent, to: S, at: datetime) -> Consent:
        if to not in TRANSITIONS[c.status]:
            raise TransitionError(f"consent {c.consent_id}: {c.status} -> {to} is not allowed")
        new = c.model_copy(update={"status": to})
        self._save(new)
        key = (c.tenant_id, c.consent_id)
        self._generation[key] = self._generation.get(key, 0) + 1
        self._event(c.tenant_id, EventKind.CONSENT_CHANGED,
                    E.ConsentChanged(consent_id=c.consent_id, status=to, previous=c.status), at)
        if to in TERMINAL:
            self._erase(new, at)
        return new

    def expire_due(self, now: datetime) -> list[str]:
        """Move every live consent past its expires_at to expired. Returns their ids."""
        with self._lock:
            due = []
            for tenant in self.tables.tenants("ops.consents"):
                for r in self.consents(tenant).iter_rows(named=True):
                    c = Consent.model_validate(r)
                    if c.status not in TERMINAL and now >= c.expires_at:
                        self._move(c, S.EXPIRED, now)
                        due.append(c.consent_id)
            return due

    # ---------------------------------------------------------------- erasure

    def _erase(self, c: Consent, at: datetime) -> None:
        payloads = self.tables.read("raw.aa_payloads", c.tenant_id)
        mine = payloads.filter(pl.col("consent_id") == c.consent_id)
        for pid in mine["payload_id"]:
            self.raw.path(c.tenant_id, pid).unlink(missing_ok=True)
        rest = payloads.filter(pl.col("consent_id") != c.consent_id)
        self.tables.replace("raw.aa_payloads", rest, c.tenant_id)
        live = self.consents(c.tenant_id).filter(~pl.col("status").is_in([s.value for s in TERMINAL]))
        destroyed = 0
        if live.is_empty():
            destroyed = int(self.keys.destroy(c.tenant_id))
            self.tables.erase_tenant(c.tenant_id, keep=("ops.consents", "ops.events"))
        self._event(c.tenant_id, EventKind.ERASURE_COMPLETED,
                    E.ErasureCompleted(consent_id=c.consent_id, keys_destroyed=destroyed), at)

    # ---------------------------------------------------------------- fetch gate

    def begin_fetch(self, tenant_id: str, consent_id: str) -> FetchTicket:
        with self._lock:
            c = self.get(tenant_id, consent_id)
            if c.status != S.ACTIVE:
                raise TransitionError(f"consent {consent_id} is {c.status}; fetching needs active")
            return FetchTicket(tenant_id, consent_id, self._generation.get((tenant_id, consent_id), 0))

    def fetch_failed(self, ticket: FetchTicket, fip_id: str, attempt: int, error: str, at: datetime) -> None:
        with self._lock:
            self._event(ticket.tenant_id, EventKind.FETCH_FAILED,
                        E.FetchFailed(consent_id=ticket.consent_id, fip_id=fip_id, attempt=attempt, error=error), at)

    def commit_fetch(self, ticket: FetchTicket, payloads: list[Fetched]) -> CommitResult:
        with self._lock:
            c = self.get(ticket.tenant_id, ticket.consent_id)
            current = self._generation.get((ticket.tenant_id, ticket.consent_id), 0)
            if c.status != S.ACTIVE or current != ticket.generation:
                return CommitResult((), (), tuple(p.payload_id for p in payloads))
            have = self.tables.read("raw.aa_payloads", ticket.tenant_id)
            seen_sha, seen_id = set(have["sha256"]), set(have["payload_id"])
            stored, dups, rows = [], [], []
            for p in payloads:
                if p.sha256 in seen_sha or p.payload_id in seen_id:
                    dups.append(p.payload_id)
                    continue
                uri, sha = self.raw.put(ticket.tenant_id, p.payload_id, p.data)
                rows.append(AaPayload(tenant_id=ticket.tenant_id, payload_id=p.payload_id, consent_id=c.consent_id,
                                      fip_id=c.fip_id, fetched_at=p.fetched_at, object_uri=uri, sha256=sha))
                seen_sha.add(sha)
                seen_id.add(p.payload_id)
                stored.append(p.payload_id)
            if rows:
                self.tables.append("raw.aa_payloads", frame(AaPayload, rows), ticket.tenant_id)
                for r in rows:
                    self._event(ticket.tenant_id, EventKind.PAYLOAD_FETCHED,
                                E.PayloadFetched(payload_id=r.payload_id, consent_id=r.consent_id, fip_id=r.fip_id,
                                                 sha256=r.sha256), r.fetched_at)
            return CommitResult(tuple(stored), tuple(dups), ())
