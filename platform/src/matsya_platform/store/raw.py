"""A3 raw store: every original payload byte, encrypted under its tenant's key.

Each object is AES-256-GCM with a random 96-bit nonce and the tenant id and payload id as
associated data, so an object cannot be decrypted under another tenant's key or replayed
under another payload id. Objects live at raw/<tenant>/<payload_id>.bin as nonce || ciphertext.
Erasure is the key: once KeyStore.destroy() runs, every object for that tenant, including
any copied elsewhere, is unreadable.
"""

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from matsya_platform.store.keys import KeyStore, Unreadable  # noqa: F401 (re-exported)


def _aad(tenant_id: str, payload_id: str) -> bytes:
    return f"{tenant_id}|{payload_id}".encode()


class RawStore:
    def __init__(self, root: str | Path, keys: KeyStore):
        self.dir = Path(root) / "raw"
        self.keys = keys

    def path(self, tenant_id: str, payload_id: str) -> Path:
        return self.dir / tenant_id / f"{payload_id}.bin"

    def put(self, tenant_id: str, payload_id: str, data: bytes) -> tuple[str, str]:
        """Encrypt and store. Returns (object_uri, sha256 of the plaintext)."""
        key = self.keys.tenant_key(tenant_id, create=True)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, data, _aad(tenant_id, payload_id))
        p = self.path(tenant_id, payload_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(p)
        return p.as_uri(), hashlib.sha256(data).hexdigest()

    def delete(self, tenant_id: str, payload_id: str) -> bool:
        """Remove one stored object. Returns whether it existed. This removes the ciphertext
        only; copies elsewhere stay unreadable through the tenant key, not through this."""
        p = self.path(tenant_id, payload_id)
        if not p.exists():
            return False
        p.unlink()
        return True

    def get(self, tenant_id: str, payload_id: str) -> bytes:
        blob = self.path(tenant_id, payload_id).read_bytes()
        try:
            key = self.keys.tenant_key(tenant_id)
        except Exception as e:
            raise Unreadable(f"{tenant_id}/{payload_id}: {e}") from e
        try:
            return AESGCM(key).decrypt(blob[:12], blob[12:], _aad(tenant_id, payload_id))
        except InvalidTag as e:
            raise Unreadable(f"{tenant_id}/{payload_id}: authentication failed") from e

    def objects(self) -> list[tuple[str, str]]:
        """(tenant_id, payload_id) for every stored object, whether still readable or not."""
        return sorted((p.parent.name, p.stem) for p in self.dir.glob("*/*.bin"))
