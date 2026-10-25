"""A3 raw store: every original payload byte, encrypted under its tenant's key.

Each object is AES-256-GCM with a random 96-bit nonce and the tenant id and payload id as
associated data, so an object cannot be decrypted under another tenant's key or replayed
under another payload id. Objects live at raw/<tenant>/<payload_id>.bin as nonce || ciphertext,
in the object store (objects.py: a local directory or an S3 bucket such as MinIO).
Erasure is the key: once KeyStore.destroy() runs, every object for that tenant, including
any copied elsewhere, is unreadable.
"""

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from matsya_platform.store.keys import KeyStore, Unreadable  # noqa: F401 (re-exported)
from matsya_platform.store.objects import ObjectStore, open_objects


def _aad(tenant_id: str, payload_id: str) -> bytes:
    return f"{tenant_id}|{payload_id}".encode()


class RawStore:
    def __init__(self, root: str | Path, keys: KeyStore, objects: ObjectStore | None = None):
        self.keys = keys
        self.backend = objects if objects is not None else open_objects(root)

    @staticmethod
    def key(tenant_id: str, payload_id: str) -> str:
        return f"raw/{tenant_id}/{payload_id}.bin"

    def put(self, tenant_id: str, payload_id: str, data: bytes) -> tuple[str, str]:
        """Encrypt and store. Returns (object_uri, sha256 of the plaintext)."""
        key = self.keys.tenant_key(tenant_id, create=True)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, data, _aad(tenant_id, payload_id))
        k = self.key(tenant_id, payload_id)
        self.backend.put(k, blob)
        url = self.backend.url(k)
        return (url if "://" in url else Path(url).resolve().as_uri()), hashlib.sha256(data).hexdigest()

    def delete(self, tenant_id: str, payload_id: str) -> bool:
        """Remove one stored object. Returns whether it existed. This removes the ciphertext
        only; copies elsewhere stay unreadable through the tenant key, not through this."""
        return self.backend.delete(self.key(tenant_id, payload_id))

    def get(self, tenant_id: str, payload_id: str) -> bytes:
        blob = self.backend.get(self.key(tenant_id, payload_id))
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
        return sorted((k.split("/")[1], k.split("/")[2].removesuffix(".bin"))
                      for k in self.backend.list_keys("raw/") if k.count("/") == 2 and k.endswith(".bin"))
