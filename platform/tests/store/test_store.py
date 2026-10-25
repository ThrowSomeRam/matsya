import base64
import json
import os
import shutil
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest
from conftest import FIXTURES
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.enums import ConsentStatus as S, EventKind
from matsya_contracts.validate import frame
from matsya_platform.consent.service import TRANSITIONS, ConsentService, TransitionError
from matsya_platform.fetch.adapter import LocalFolderAdapter
from matsya_platform.fetch.runner import FetchGaveUp, fetch, stored_payloads
from matsya_platform.ledger.build import build
from matsya_platform.match.engine import match
from matsya_platform.store.keys import MASTER_KEY_ENV, KeyDestroyed, KeyError_, KeyStore
from matsya_platform.store.persist import persist_build, persist_match, read_build, read_match
from matsya_platform.store.raw import RawStore, Unreadable
from matsya_platform.store.tables import TableStore

UTC = timezone.utc
A = "3b2f7c1e-9a4d-4e8b-b6c5-1d2e3f4a5b6c"
B = "9c8d7e6f-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
T0 = datetime(2026, 4, 1, tzinfo=UTC)
DELIVERIES = [("april", "2026-05-01T00:30:00+00:00"), ("may", "2026-06-01T00:30:00+00:00")]


@pytest.fixture
def master(monkeypatch):
    monkeypatch.setenv(MASTER_KEY_ENV, base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def folder(tmp_path):
    """A rendered folder holding the same two fixture payloads for tenants A and B."""
    d = tmp_path / "rendered"
    (d / "payloads").mkdir(parents=True)
    deliveries = []
    for tenant, tag in ((A, "a"), (B, "b")):
        for name, at in DELIVERIES:
            pid = f"PL-{tag}-{name}"
            shutil.copy(FIXTURES / f"{name}.json", d / "payloads" / f"{pid}.json")
            deliveries.append({"payload_id": pid, "tenant_id": tenant, "fetched_at": at,
                               "file": f"payloads/{pid}.json"})
    (d / "manifest.json").write_text(json.dumps({"deliveries": deliveries}))
    return d


def services(root):
    tables, keys = TableStore(root), KeyStore(root)
    return ConsentService(tables, keys, RawStore(root, keys))


def consent(svc, tenant, cid="C1", status=S.ACTIVE, expires=T0 + timedelta(days=365)):
    svc.create(tenant_id=tenant, consent_id=cid, fip_id="FIP-1", purpose="cash-flow forecast",
               range_from=date(2025, 4, 1), range_to=date(2026, 3, 31), frequency="weekly", expires_at=expires, at=T0)
    if status != S.PENDING:
        svc.transition(tenant, cid, status, T0)


def events(svc, tenant, kind):
    ev = svc.tables.read("ops.events", tenant)
    return [json.loads(p) for p in ev.filter(pl.col("kind") == kind)["payload_json"]]


# ---------------------------------------------------------------- master key


def test_missing_master_key_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.delenv(MASTER_KEY_ENV, raising=False)
    with pytest.raises(KeyError_, match=f"{MASTER_KEY_ENV} is not set"):
        KeyStore(tmp_path)


@pytest.mark.parametrize("bad", ["not base64!", base64.b64encode(b"short").decode()])
def test_malformed_master_key_fails_loudly(monkeypatch, tmp_path, bad):
    monkeypatch.setenv(MASTER_KEY_ENV, bad)
    with pytest.raises(KeyError_, match=MASTER_KEY_ENV):
        KeyStore(tmp_path)


def test_tenant_key_is_stored_only_wrapped(master, tmp_path):
    keys = KeyStore(tmp_path)
    key = keys.tenant_key(A, create=True)
    on_disk = (tmp_path / "keys" / f"{A}.wrapped").read_bytes()
    assert key not in on_disk and len(on_disk) == 40  # RFC 3394: 32-byte key + 8-byte integrity block


def test_wrong_master_key_cannot_unwrap(master, tmp_path, monkeypatch):
    KeyStore(tmp_path).tenant_key(A, create=True)
    monkeypatch.setenv(MASTER_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(KeyError_, match="does not unwrap"):
        KeyStore(tmp_path).tenant_key(A)


# ---------------------------------------------------------------- erasure proof


def test_erasure_proof_destroyed_tenant_unreadable_other_untouched(master, tmp_path, folder):
    root = tmp_path / "store"
    svc = services(root)
    for t in (A, B):
        consent(svc, t)
        assert len(fetch(svc, LocalFolderAdapter(folder), t, "C1").commit.stored) == 2
    objects = svc.raw.objects()
    assert sorted({t for t, _ in objects}) == sorted([A, B]) and len(objects) == 4
    # A copy of A's ciphertext taken before erasure, as a backup or a stray copy would be.
    stray = {pid: svc.raw.path(A, pid).read_bytes() for t, pid in objects if t == A}

    assert svc.keys.destroy(A) is True

    # A fresh process: new KeyStore and RawStore over the same disk and master key. A soft
    # delete (a flag, or a key kept anywhere on disk) would let this one read A's data.
    keys = KeyStore(root)
    raw = RawStore(root, keys)
    assert not (root / "keys" / f"{A}.wrapped").exists()
    for tenant, pid in objects:
        if tenant == A:
            with pytest.raises(Unreadable):
                raw.get(tenant, pid)
        else:
            assert raw.get(tenant, pid) == (folder / "payloads" / f"{pid}.json").read_bytes()
    # Every key the system can still produce fails on A's ciphertext with a real decrypt,
    # including a key regenerated for A and B's surviving key.
    with pytest.raises(KeyDestroyed):
        keys.tenant_key(A)
    candidates = [keys.tenant_key(A, create=True), keys.tenant_key(B)]
    for pid, blob in stray.items():
        for k in candidates:
            for aad in (f"{A}|{pid}".encode(), f"{B}|{pid}".encode()):
                with pytest.raises(InvalidTag):
                    AESGCM(k).decrypt(blob[:12], blob[12:], aad)


def test_object_moved_to_another_tenant_cannot_be_read_there(master, tmp_path, folder):
    svc = services(tmp_path)
    for t in (A, B):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
    shutil.copy(svc.raw.path(A, "PL-a-april"), svc.raw.path(B, "PL-a-april"))
    with pytest.raises(Unreadable, match="authentication failed"):
        svc.raw.get(B, "PL-a-april")


# ---------------------------------------------------------------- consent


def test_transition_table(master, tmp_path):
    for start in S:
        for to in S:
            svc = services(tmp_path / f"{start}-{to}")
            consent(svc, A, status=S.PENDING)
            path = {S.PENDING: [], S.ACTIVE: [S.ACTIVE], S.PAUSED: [S.ACTIVE, S.PAUSED], S.REVOKED: [S.REVOKED],
                    S.EXPIRED: [S.EXPIRED]}[start]
            for step in path:
                svc.transition(A, "C1", step, T0)
            if to in TRANSITIONS[start]:
                assert svc.transition(A, "C1", to, T0).status == to
            else:
                with pytest.raises(TransitionError):
                    svc.transition(A, "C1", to, T0)
                assert svc.get(A, "C1").status == start


def test_every_change_is_an_event(master, tmp_path):
    svc = services(tmp_path)
    consent(svc, A)
    svc.transition(A, "C1", S.PAUSED, T0)
    svc.transition(A, "C1", S.ACTIVE, T0)
    seen = [(e["previous"], e["status"]) for e in events(svc, A, EventKind.CONSENT_CHANGED)]
    assert seen == [(None, "pending"), ("pending", "active"), ("active", "paused"), ("paused", "active")]


def test_past_expiry_expires_instead_and_erases(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A, expires=T0 + timedelta(days=30))
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    with pytest.raises(TransitionError, match="expired"):
        svc.transition(A, "C1", S.PAUSED, T0 + timedelta(days=31))
    assert svc.get(A, "C1").status == S.EXPIRED
    assert svc.raw.objects() == [] and events(svc, A, EventKind.ERASURE_COMPLETED)[0]["keys_destroyed"] == 1


def test_expire_due_sweeps_overdue_consents(master, tmp_path):
    svc = services(tmp_path)
    consent(svc, A, "C1", expires=T0 + timedelta(days=10))
    consent(svc, B, "C2", expires=T0 + timedelta(days=100))
    assert svc.expire_due(T0 + timedelta(days=11)) == ["C1"]
    assert (svc.get(A, "C1").status, svc.get(B, "C2").status) == (S.EXPIRED, S.ACTIVE)


def test_revoke_erases_raw_key_and_derived_rows_for_that_tenant_only(master, tmp_path, folder):
    root = tmp_path / "store"
    svc = services(root)
    for t in (A, B):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
        persist_build(svc.tables, build(stored_payloads(svc, t)), t, "run-1")
    blobs = {pid: svc.raw.path(A, pid).read_bytes() for t, pid in svc.raw.objects() if t == A}
    b_txns = svc.tables.read("ledger.transactions", B)

    svc.transition(A, "C1", S.REVOKED, T0 + timedelta(days=1))

    assert [t for t, _ in svc.raw.objects()] == [B, B]
    assert svc.tables.read("ledger.transactions", A).is_empty() and svc.tables.read("raw.aa_payloads", A).is_empty()
    assert svc.tables.read("ledger.transactions", B).equals(b_txns)
    assert svc.get(A, "C1").status == S.REVOKED  # the consent record itself is kept
    assert events(svc, A, EventKind.ERASURE_COMPLETED) == [{"consent_id": "C1", "keys_destroyed": 1}]
    keys = KeyStore(root)  # a stray copy of the ciphertext is still unreadable
    with pytest.raises(KeyDestroyed):
        keys.tenant_key(A)
    for pid, blob in blobs.items():
        with pytest.raises(InvalidTag):
            AESGCM(keys.tenant_key(A, create=True)).decrypt(blob[:12], blob[12:], f"{A}|{pid}".encode())


def test_revoke_with_another_live_consent_keeps_the_key(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A, "C1")
    consent(svc, A, "C2")
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    svc.transition(A, "C1", S.REVOKED, T0)
    assert svc.raw.objects() == [] and svc.tables.read("raw.aa_payloads", A).is_empty()
    assert svc.keys.tenants() == [A]
    assert events(svc, A, EventKind.ERASURE_COMPLETED) == [{"consent_id": "C1", "keys_destroyed": 0}]


class _ChangesConsentMidFetch(LocalFolderAdapter):
    def __init__(self, folder, svc, to):
        super().__init__(folder)
        self.svc, self.to = svc, to

    def fetch(self, consent, attempt):
        out = super().fetch(consent, attempt)  # the payload is in hand ...
        self.svc.transition(consent.tenant_id, consent.consent_id, self.to, T0)  # ... when the owner acts
        return out


@pytest.mark.parametrize("to", [S.REVOKED, S.PAUSED])
def test_consent_change_during_fetch_discards_the_in_flight_payload(master, tmp_path, folder, to):
    svc = services(tmp_path)
    consent(svc, A)
    r = fetch(svc, _ChangesConsentMidFetch(folder, svc, to), A, "C1")
    assert r.commit.stored == () and sorted(r.commit.discarded) == ["PL-a-april", "PL-a-may"]
    assert svc.raw.objects() == [] and svc.tables.read("raw.aa_payloads", A).is_empty()
    assert events(svc, A, EventKind.PAYLOAD_FETCHED) == []


def test_pause_then_resume_mid_fetch_still_discards(master, tmp_path, folder):
    """The generation check: active again at commit, but not the same activation."""
    svc = services(tmp_path)
    consent(svc, A)

    class Flap(LocalFolderAdapter):
        def fetch(self, consent, attempt):
            out = super().fetch(consent, attempt)
            svc.transition(A, "C1", S.PAUSED, T0)
            svc.transition(A, "C1", S.ACTIVE, T0)
            return out

    assert fetch(svc, Flap(folder), A, "C1").commit.stored == ()


def test_fetch_needs_an_active_consent(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A, status=S.PENDING)
    with pytest.raises(TransitionError, match="needs active"):
        fetch(svc, LocalFolderAdapter(folder), A, "C1")


# ---------------------------------------------------------------- fetch


def test_retries_then_succeeds_and_records_failures(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A)
    slept = []
    r = fetch(svc, LocalFolderAdapter(folder, fail_attempts=2), A, "C1", retries=3, sleep=slept.append,
              now=lambda: T0)
    assert r.attempts == 3 and slept == [1.0, 2.0] and len(r.commit.stored) == 2
    assert [e["attempt"] for e in events(svc, A, EventKind.FETCH_FAILED)] == [1, 2]


def test_gives_up_after_the_last_retry(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A)
    with pytest.raises(FetchGaveUp):
        fetch(svc, LocalFolderAdapter(folder, fail_attempts=3), A, "C1", retries=3, sleep=lambda s: None, now=lambda: T0)
    assert svc.raw.objects() == []


def test_refetch_is_idempotent_by_payload_hash(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    again = fetch(svc, LocalFolderAdapter(folder), A, "C1")
    assert again.commit.stored == () and sorted(again.commit.duplicates) == ["PL-a-april", "PL-a-may"]
    # The same bytes under a new payload id are the same payload.
    m = json.loads((folder / "manifest.json").read_text())
    m["deliveries"] = [dict(d, payload_id=d["payload_id"] + "-redelivered") for d in m["deliveries"]]
    (folder / "manifest.json").write_text(json.dumps(m))
    assert fetch(svc, LocalFolderAdapter(folder), A, "C1").commit.stored == ()
    assert svc.tables.read("raw.aa_payloads", A).height == 2


# ---------------------------------------------------------------- persistence


def test_build_round_trips_through_the_store_and_build_stays_pure(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    payloads = stored_payloads(svc, A)
    direct = build([p for p in payloads])
    again = build(payloads)  # same input, same output: persistence did not leak into build
    assert direct.txns.equals(again.txns)
    written = persist_build(svc.tables, direct, A, "run-1")
    back = read_build(svc.tables, A)
    # ops.events also holds the consent service's events; compare the build's own.
    back["events"] = back["events"].filter(pl.col("event_id").is_in(direct.events["event_id"].implode()))
    for attr in ("txns", "balances", "quarantine", "events"):
        key = [c for c in getattr(direct, attr).columns][:2]
        assert back[attr].sort(key).equals(getattr(direct, attr).sort(key)), attr
    assert back["coverage"].drop("run_id").equals(direct.coverage)
    assert written["ledger.transactions"] == direct.txns.height > 0
    q = svc.tables.query("select count(*) as n, sum(amount_paise) as s from ledger.transactions")
    assert q.row(0) == (direct.txns.height, direct.txns["amount_paise"].sum())


def test_match_result_round_trips_through_the_store(master, tmp_path):
    from matsya_platform.ingest.invoices import fiscal_year
    from matsya_platform.match.buyers import gstin_checksum

    pan, sup = "ABCCA1234F", "29AAFCS1234K1Z2"
    first14 = f"27{pan}1Z"
    acme = T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=first14 + gstin_checksum(first14),
                   name="ACME TEXTILES LTD", type="listed", sector="textiles", sector_source="exchange",
                   treds_mandated=None)
    d = date(2025, 5, 1)
    invs = [T.Invoice(tenant_id=A, invoice_id=ids.invoice_id(A, sup, no, fiscal_year(d)), irn=None,
                      buyer_id=acme.buyer_id, issue_date=d, due_date=d + timedelta(days=30), terms_days=30,
                      terms_source="document_terms", gross_paise=g, gst_paise=18_000_00, expected_tds_paise=None,
                      status="open", known_at=datetime(2025, 5, 1, 12, tzinfo=UTC))
            for no, g in (("INV-1", 118_000_50), ("INV-2", 236_000_10))]
    cr = [T.Transaction(tenant_id=A, txn_uid=ids.hash32("c", str(i)), account_id="ACC", value_date=d + timedelta(days=20),
                        known_at=datetime(2025, 5, 22, tzinfo=UTC), amount_paise=amt, direction="credit",
                        narration_raw=f"BY NEFT/X{i}/ACME TEXTILES LTD/PAYMENT", source="aa", dq_score=1.0,
                        dup_group_id=None)
          for i, amt in enumerate((116_000_49, 236_000_10 - 600_00))]
    r = match(frame(T.Transaction, cr), frame(T.Invoice, invs), frame(T.Buyer, [acme]), None, tenant_id=A,
              supplier_gstin=sup, as_of=datetime(2025, 8, 31, tzinfo=UTC), complete_through=date(2025, 8, 30))
    store = TableStore(tmp_path)
    persist_match(store, r, A, "match-1")
    back = read_match(store, A)
    assert back["matches"].sort("txn_uid").equals(r.matches.sort("txn_uid"))
    assert back["outcomes"].sort("invoice_id").equals(r.outcomes.sort("invoice_id"))
    assert back["links"].drop("run_id", "tenant_id").sort("txn_uid").equals(r.links.sort("txn_uid"))
    assert dict(zip(back["label_evidence"]["invoice_id"], back["label_evidence"]["evidence"])) == r.label_evidence
    assert back["review"].height == len(r.review_ranked)
    assert [json.loads(j)["txn_uid"] for j in back["review"].sort("position")["item_json"]] == \
        [x["txn_uid"] for x in r.review_ranked]


def test_contract_tables_are_validated_on_write(master, tmp_path):
    store = TableStore(tmp_path)
    with pytest.raises(Exception):
        store.append("ledger.transactions", pl.DataFrame({"tenant_id": [A], "txn_uid": ["x"]}), A)
    with pytest.raises(ValueError, match="other than"):
        store.append("aux.coverage", pl.DataFrame({"tenant_id": [B]}), A)
