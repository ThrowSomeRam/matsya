import base64
import json
import os
import shutil
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest
from conftest import FIXTURES
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from matsya_contracts import ids
from matsya_contracts import tables as T
from matsya_contracts.enums import ConsentStatus as S, EventKind
from matsya_contracts.validate import frame, validate
from matsya_platform.consent.service import TRANSITIONS, ConsentService, TransitionError
from matsya_platform.fetch.adapter import LocalFolderAdapter
from matsya_platform.fetch.runner import FetchGaveUp, fetch, stored_payloads
from matsya_platform.ledger.build import build
from matsya_platform.match.engine import match
from matsya_platform.store.keys import MASTER_KEY_ENV, PLATFORM, KeyDestroyed, KeyError_, KeyStore
from matsya_platform.store.objects import (S3_ACCESS_KEY_ENV, S3_BUCKET_ENV, S3_ENDPOINT_ENV, S3_PREFIX_ENV,
                                           S3_SECRET_KEY_ENV, S3Objects, StoreConfigError)
from matsya_platform.store.persist import persist_build, persist_match, read_build, read_match
from matsya_platform.store.raw import RawStore, Unreadable
from matsya_platform.store.tables import TableStore

UTC = timezone.utc
A = "3b2f7c1e-9a4d-4e8b-b6c5-1d2e3f4a5b6c"
B = "9c8d7e6f-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
T0 = datetime(2026, 4, 1, tzinfo=UTC)
DELIVERIES = [("april", "2026-05-01T00:30:00+00:00"), ("may", "2026-06-01T00:30:00+00:00")]


# MinIO runs: set MATSYA_TEST_S3_ENDPOINT, _BUCKET, _ACCESS_KEY and _SECRET_KEY (an unversioned
# bucket; infra/compose.platform.yml makes one). Without them the s3 cases skip.
S3_TEST = {v: os.environ.get(v.replace("MATSYA_", "MATSYA_TEST_"))
           for v in (S3_ENDPOINT_ENV, S3_BUCKET_ENV, S3_ACCESS_KEY_ENV, S3_SECRET_KEY_ENV)}


@pytest.fixture(autouse=True, params=["local", "s3"])
def backend(request, monkeypatch):
    """Every test runs against a local folder and, when configured, against MinIO, under a
    prefix of its own that is removed afterwards."""
    if request.param == "local":
        monkeypatch.delenv(S3_ENDPOINT_ENV, raising=False)
        yield "local"
        return
    if not all(S3_TEST.values()):
        pytest.skip("MinIO not configured (MATSYA_TEST_S3_*)")
    prefix = f"pytest-{uuid.uuid4().hex[:12]}"
    for k, v in {**S3_TEST, S3_PREFIX_ENV: prefix}.items():
        monkeypatch.setenv(k, v)
    yield "s3"
    S3Objects(S3_TEST[S3_ENDPOINT_ENV], S3_TEST[S3_BUCKET_ENV], S3_TEST[S3_ACCESS_KEY_ENV],
              S3_TEST[S3_SECRET_KEY_ENV], prefix=prefix).clear()


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
    keys = KeyStore(root)
    return ConsentService(TableStore(root, keys), keys, RawStore(root, keys))


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
    stray = {pid: svc.raw.backend.get(RawStore.key(A, pid)) for t, pid in objects if t == A}

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
    svc.raw.backend.put(RawStore.key(B, "PL-a-april"), svc.raw.backend.get(RawStore.key(A, "PL-a-april")))
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
    blobs = {pid: svc.raw.backend.get(RawStore.key(A, pid)) for t, pid in svc.raw.objects() if t == A}
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
    assert A in svc.keys.tenants()
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


# ---------------------------------------------------------------- blockers: delete, shared tables, export


class _RecordingRaw(RawStore):
    def __init__(self, *a):
        super().__init__(*a)
        self.deleted = []

    def delete(self, tenant_id, payload_id):
        self.deleted.append((tenant_id, payload_id))
        return super().delete(tenant_id, payload_id)


def test_erasure_deletes_raw_objects_through_the_store(master, tmp_path, folder):
    keys = KeyStore(tmp_path)
    tables, raw = TableStore(tmp_path, keys), _RecordingRaw(tmp_path, keys)
    svc = ConsentService(tables, keys, raw)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    svc.transition(A, "C1", S.REVOKED, T0)
    assert sorted(raw.deleted) == [(A, "PL-a-april"), (A, "PL-a-may")] and raw.objects() == []
    assert raw.delete(A, "PL-a-april") is False


def test_shared_reference_table_survives_a_tenant_erasure(master, tmp_path):
    store = TableStore(tmp_path)
    pan = "ABCCA1234F"
    b = frame(T.Buyer, [T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=None, name="ACME",
                                type="listed", sector="textiles", sector_source="exchange", treds_mandated=None)])
    store.replace("ref.buyers", b)
    assert store.read("ref.buyers").equals(b) and store.read("ref.buyers", A).equals(b)
    store.append("aux.coverage", pl.DataFrame({"tenant_id": [A], "x": [1]}), A)
    store.erase_tenant(A)
    assert store.read("ref.buyers").equals(b) and store.read("aux.coverage", A).is_empty()
    with pytest.raises(ValueError, match="per tenant"):
        store.append("ledger.transactions", store.read("ledger.transactions"))


def test_export_is_the_flat_layout_track_b_reads(master, tmp_path, folder):
    from matsya_platform.store.export import EXPORTED, export_contract_dir

    svc = services(tmp_path / "store")
    for t in (A, B):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
        persist_build(svc.tables, build(stored_payloads(svc, t)), t, "run-1")
    written = export_contract_dir(svc.tables, tmp_path / "contract")
    files = sorted(p.name for p in (tmp_path / "contract").iterdir())
    assert files == sorted([*(f"{t}.parquet" for t in EXPORTED), "_export.json"])  # every A table, empty ones too
    # Track B's reader (intelligence io.source.read_contract_dir): read_parquet + validate per table.
    for t in ("ledger.transactions", "ledger.balances", "ledger.invoices", "ledger.matches", "labels.invoice_outcomes",
              "labels.txn_corrections", "ref.buyers", "ref.tenant_buyers"):
        df = pl.read_parquet(tmp_path / "contract" / f"{t}.parquet")
        validate(df, t, sample=None)
        assert df.height == written[t]
    tx = pl.read_parquet(tmp_path / "contract" / "ledger.transactions.parquet")
    assert set(tx["tenant_id"]) == {A, B} and tx.height == written["ledger.transactions"] > 0



# ---------------------------------------------------------------- erasure proof: raw and derived


def _all_tenant_objects(backend, tenant):
    """Every stored object of the tenant: raw payloads and table parts (not purge markers)."""
    return sorted([*backend.list_keys(f"raw/{tenant}/"),
                   *(k for k in backend.list_keys("tables/") if k.split("/")[3] == tenant and k.endswith(".parquet"))])


def _parquet_decrypts(blob: bytes, key: bytes, tmp) -> bool:
    """A real decrypt of one encrypted Parquet part under key, by a fresh DuckDB."""
    f = tmp / f"probe-{uuid.uuid4().hex}.parquet"
    f.write_bytes(blob)
    con = duckdb.connect()
    con.execute(f"PRAGMA add_parquet_key('k', '{base64.b64encode(key).decode()}')")
    try:
        con.sql(f"SELECT count(*) FROM read_parquet('{f}', encryption_config = {{footer_key: 'k'}})").fetchall()
        return True
    except duckdb.InvalidInputException as e:
        assert "AES tag" in str(e)
        return False
    finally:
        con.close()
        f.unlink()


def test_erasure_proof_covers_raw_and_derived_tables(master, tmp_path, folder):
    """Both tenants hold raw payloads and derived tables (ledger rows, balances, events).
    Destroy A's key; from a fresh process, every A object, raw or derived, fails a real
    decrypt under a regenerated A key, B's key and the platform key; B reads back unchanged;
    no object anywhere is plaintext; and under MinIO no old object version is left."""
    root = tmp_path / "store"
    svc = services(root)
    for t in (A, B):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
        persist_build(svc.tables, build(stored_payloads(svc, t)), t, "run-1")
    narration = svc.tables.read("ledger.transactions", A)["narration_raw"][0].encode()
    svc.tables.query("select count(*) from ledger.transactions")
    backend = svc.tables.backend
    every = _all_tenant_objects(backend, A)
    # The platform-key parts are A's consent record and lifecycle events, kept on purpose and
    # checked below; every other A object is under A's key and must become noise.
    audit = [k for k in every if k.endswith(".p.parquet")]
    a_objects = [k for k in every if k not in audit]
    assert {k.split("/")[1] for k in a_objects if k.startswith("tables/")} >= {"ledger", "ops", "raw", "aux"}
    stray = {k: backend.get(k) for k in a_objects}  # copies taken before erasure
    b_before = {t: svc.tables.read(t, B) for t in ("ledger.transactions", "ledger.balances", "raw.aa_payloads")}

    assert svc.keys.destroy(A) is True

    keys = KeyStore(root)  # a fresh process: nothing cached
    tables, raw = TableStore(root, keys), RawStore(root, keys)
    for t in ("ledger.transactions", "ledger.balances", "raw.aa_payloads", "aux.coverage"):
        with pytest.raises(Unreadable):
            tables.read(t, A)
    for _, pid in [o for o in raw.objects() if o[0] == A]:
        with pytest.raises(Unreadable):
            raw.get(A, pid)
    with pytest.raises(Unreadable):
        tables.query("select count(*) from ledger.transactions")  # A's parts poison a cross-tenant query
    candidates = [keys.tenant_key(A, create=True), keys.tenant_key(B), keys.tenant_key(PLATFORM)]
    for k, blob in stray.items():
        for key in candidates:
            if k.startswith("tables/"):
                assert not _parquet_decrypts(blob, key, tmp_path), k
            else:
                pid = k.rsplit("/", 1)[-1].removesuffix(".bin")
                for aad in (f"{A}|{pid}".encode(), f"{B}|{pid}".encode()):
                    with pytest.raises(InvalidTag):
                        AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    for t, df in b_before.items():
        assert tables.read(t, B).equals(df), t
    assert {".".join(k.split("/")[1:3]) for k in audit} == {"ops.consents", "ops.events"}
    assert set(tables.read("ops.consents", A)["consent_id"]) == {"C1"}
    with pytest.raises(Unreadable):  # A's events table mixes both keys: its data events are noise
        tables.read("ops.events", A)
    stored = [backend.get(k) for k in backend.list_keys("")]
    stored += [p.read_bytes() for p in root.rglob("*") if p.is_file()]  # local objects, keys, nothing else
    assert stored and not any(b.startswith(b"PAR1") or narration in b for b in stored)
    if isinstance(backend, S3Objects):
        assert {p.relative_to(root).parts[0] for p in root.rglob("*") if p.is_file()} == {"keys"}
        assert svc.tables.erase_tenant(A) and backend.versions() == backend.list_keys("")


def test_destroyed_key_does_not_survive_in_duckdb(master, tmp_path, folder):
    """The table store hands keys to DuckDB; a destroy must reach that copy too."""
    svc = services(tmp_path)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    persist_build(svc.tables, build(stored_payloads(svc, A)), A, "run-1")
    assert svc.tables.read("ledger.transactions", A).height > 0  # A's key is now inside DuckDB
    svc.keys.destroy(A)
    with pytest.raises(Unreadable):
        svc.tables.read("ledger.transactions", A)


def test_every_commit_is_a_snapshot_and_reads_as_of_return_it(master, tmp_path):
    """Reproducibility: a reader that records when it read gets the same rows again later,
    after appends and replaces, from a fresh process."""
    store = TableStore(tmp_path)
    store.append("aux.coverage", pl.DataFrame({"x": [1]}), A)
    store.append("aux.coverage", pl.DataFrame({"x": [2]}), A)
    t1 = datetime.now(UTC)
    first = store.read("aux.coverage", A)
    time.sleep(0.01)
    store.replace("aux.coverage", pl.DataFrame({"x": [10]}), A)
    store.append("aux.coverage", pl.DataFrame({"x": [11]}), A)
    store.append("aux.coverage", pl.DataFrame({"x": [7]}), B)
    again = TableStore(tmp_path, KeyStore(tmp_path))
    assert again.read("aux.coverage", A)["x"].to_list() == [10, 11]
    assert again.read("aux.coverage", A, as_of=t1).equals(first) and first["x"].to_list() == [1, 2]
    assert again.read("aux.coverage", B, as_of=t1).is_empty()
    assert again.read("aux.coverage", A, as_of=datetime(2000, 1, 1, tzinfo=UTC)).is_empty()
    assert again.query("select sum(x) as s from aux.coverage", as_of=t1).item() == 3
    assert again.query("select sum(x) as s from aux.coverage").item() == 28


def test_reference_buyers_keep_their_history(master, tmp_path):
    """ref.buyers is replaced on every run; the version a forecast used stays readable."""
    store = TableStore(tmp_path)
    pan = "ABCCA1234F"
    buyer = T.Buyer(buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=None, name="ACME", type="listed",
                    sector="textiles", sector_source="exchange", treds_mandated=None)
    store.replace("ref.buyers", frame(T.Buyer, [buyer]))
    t1 = datetime.now(UTC)
    time.sleep(0.01)
    store.replace("ref.buyers", frame(T.Buyer, [T.Buyer.model_validate({**buyer.model_dump(), "sector": "fmcg"})]))
    assert store.read("ref.buyers")["sector"].to_list() == ["fmcg"]
    assert store.read("ref.buyers", as_of=t1)["sector"].to_list() == ["textiles"]


def test_revoking_one_of_two_consents_purges_its_rows_from_history(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A, "C1")
    consent(svc, A, "C2")
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    t1 = datetime.now(UTC)
    assert svc.tables.read("raw.aa_payloads", A, as_of=t1).height == 2
    svc.transition(A, "C1", S.REVOKED, T0)
    assert svc.tables.read("raw.aa_payloads", A).is_empty()
    with pytest.raises(Unreadable, match="purged"):
        svc.tables.read("raw.aa_payloads", A, as_of=t1)
    assert len(svc.tables.backend.list_keys(f"tables/raw/aa_payloads/{A}/")) == 2  # the empty snapshot, the marker


def test_an_export_can_be_made_again_as_of_its_time(master, tmp_path, folder):
    """The reproducibility seam: Track B reads exports. One made at t is made again, file for
    file, after the store has moved on; a purge in between makes it refuse, not differ."""
    from matsya_platform.store.export import MANIFEST, export_contract_dir

    svc = services(tmp_path / "store")

    def onboard(t):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
        persist_build(svc.tables, build(stored_payloads(svc, t)), t, "run-1")

    onboard(A)
    export_contract_dir(svc.tables, tmp_path / "e1")
    first = json.loads((tmp_path / "e1" / MANIFEST).read_text())
    onboard(B)  # the store moves on
    svc.tables.replace("ref.buyers", svc.tables.read("ref.buyers"))
    export_contract_dir(svc.tables, tmp_path / "e2", as_of=datetime.fromisoformat(first["store_as_of"]))
    assert json.loads((tmp_path / "e2" / MANIFEST).read_text()) == first
    assert first["tables"]["ledger.transactions"]["rows"] > 0
    export_contract_dir(svc.tables, tmp_path / "e3")
    assert json.loads((tmp_path / "e3" / MANIFEST).read_text())["tables"]["ledger.transactions"]["rows"] > \
        first["tables"]["ledger.transactions"]["rows"]
    svc.transition(A, "C1", S.REVOKED, T0 + timedelta(days=1))
    with pytest.raises(Unreadable, match="purged"):
        export_contract_dir(svc.tables, tmp_path / "e4", as_of=datetime.fromisoformat(first["store_as_of"]))


def test_erased_tenant_is_gone_from_old_snapshots_too(master, tmp_path, folder):
    svc = services(tmp_path)
    for t in (A, B):
        consent(svc, t)
        fetch(svc, LocalFolderAdapter(folder), t, "C1")
        persist_build(svc.tables, build(stored_payloads(svc, t)), t, "run-1")
    t1 = datetime.now(UTC)
    b_then = svc.tables.read("ledger.transactions", B, as_of=t1)
    svc.transition(A, "C1", S.REVOKED, T0 + timedelta(days=1))
    with pytest.raises(Unreadable):
        svc.tables.read("ledger.transactions", A, as_of=t1)
    assert svc.tables.read("ledger.transactions", B, as_of=t1).equals(b_then) and b_then.height > 0


def test_query_reads_the_encrypted_parts_in_place(master, tmp_path, folder):
    svc = services(tmp_path)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    persist_build(svc.tables, build(stored_payloads(svc, A)), A, "run-1")
    n = svc.tables.query("select count(*) from ledger.transactions").item()
    (view,) = svc.tables._connection().sql(
        "select sql from duckdb_views() where schema_name = 'ledger' and view_name = 'transactions'").fetchall()[0]
    (part,) = svc.tables.files("ledger.transactions", A)
    assert n > 0 and svc.tables.backend.url(part) in view and "encryption_config" in view


def test_s3_refuses_a_bucket_that_keeps_old_versions(backend):
    if backend != "s3":
        pytest.skip("MinIO only")
    import boto3

    c = boto3.client("s3", endpoint_url=S3_TEST[S3_ENDPOINT_ENV], aws_access_key_id=S3_TEST[S3_ACCESS_KEY_ENV],
                     aws_secret_access_key=S3_TEST[S3_SECRET_KEY_ENV], region_name="us-east-1")
    bucket = f"matsya-versioned-{uuid.uuid4().hex[:8]}"
    c.create_bucket(Bucket=bucket)
    try:
        c.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        with pytest.raises(StoreConfigError, match="versioning"):
            S3Objects(S3_TEST[S3_ENDPOINT_ENV], bucket, S3_TEST[S3_ACCESS_KEY_ENV], S3_TEST[S3_SECRET_KEY_ENV])
    finally:
        c.delete_bucket(Bucket=bucket)


def test_a_consent_record_survives_the_erasure_it_triggers(master, tmp_path, folder):
    root = tmp_path / "store"
    svc = services(root)
    consent(svc, A)
    fetch(svc, LocalFolderAdapter(folder), A, "C1")
    persist_build(svc.tables, build(stored_payloads(svc, A)), A, "run-1")
    svc.transition(A, "C1", S.REVOKED, T0)
    keys = KeyStore(root)
    tables = TableStore(root, keys)
    assert tables.read("ops.consents", A)["status"].to_list() == ["revoked"]
    kinds = set(tables.read("ops.events", A)["kind"])
    assert kinds == {"consent_changed", "erasure_completed"}  # payload_fetched went with the tenant key
    left = _all_tenant_objects(tables.backend, A)
    assert left and all(k.startswith("tables/ops/") and k.endswith(".p.parquet") for k in left)


def test_service_refuses_stores_with_their_own_key_caches(master, tmp_path):
    keys = KeyStore(tmp_path)
    with pytest.raises(ValueError, match="share one KeyStore"):
        ConsentService(TableStore(tmp_path), keys, RawStore(tmp_path, keys))


def test_reference_buyers_are_under_the_platform_key(master, tmp_path):
    store = TableStore(tmp_path)
    pan = "ABCCA1234F"
    store.replace("ref.buyers", frame(T.Buyer, [T.Buyer(
        buyer_id=ids.buyer_id(pan), id_basis="pan", pan=pan, gstin=None, name="ACME", type="listed",
        sector="textiles", sector_source="exchange", treds_mandated=None)]))
    (k,) = store.files("ref.buyers")
    blob = store.backend.get(k)
    assert k.endswith(".p.parquet") and blob.startswith(b"PARE") and b"ACME" not in blob
