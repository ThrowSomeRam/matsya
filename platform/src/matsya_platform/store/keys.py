"""Per-tenant data keys, wrapped by a master key (A3).

The master key comes only from the environment variable MATSYA_MASTER_KEY: 32 random bytes,
base64-encoded (see .env.example). It is never a literal in code and never committed; if it
is unset or malformed, KeyStore refuses to start rather than falling back to a default.

Keys stay on local disk and never go to the object store (objects.py): a copy of a wrapped key
there, in an old object version or a replica, plus the master key would undo an erasure.

Each tenant gets a random 256-bit data key, stored on disk only in wrapped form (RFC 3394
AES key wrap under the master key). destroy() overwrites the wrapped key file with random
bytes, deletes it, and drops any cached copy: there is no "destroyed" flag to flip back, and
without the key nothing encrypted under it can be decrypted again.
"""

import base64
import binascii
import os
from pathlib import Path

from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

MASTER_KEY_ENV = "MATSYA_MASTER_KEY"
# The platform's own data key, for what must outlive any one tenant: the shared reference
# tables, and the record that a tenant's consent existed and was ended. Never destroyed by a
# tenant's erasure. Tenant ids are UUIDs, so this name cannot collide with one.
PLATFORM = "_platform"


class KeyError_(RuntimeError):
    pass


class KeyDestroyed(KeyError_):
    pass


class Unreadable(RuntimeError):
    """A stored object (raw payload or table part) that cannot be decrypted: its key is gone,
    or the object was altered or moved."""


def master_key_from_env() -> bytes:
    raw = os.environ.get(MASTER_KEY_ENV)
    if not raw:
        raise KeyError_(f"{MASTER_KEY_ENV} is not set. Set it to 32 random bytes, base64-encoded "
                        "(see .env.example); there is no default.")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise KeyError_(f"{MASTER_KEY_ENV} is not valid base64") from e
    if len(key) != 32:
        raise KeyError_(f"{MASTER_KEY_ENV} must decode to 32 bytes, got {len(key)}")
    return key


class KeyStore:
    def __init__(self, root: str | Path, master_key: bytes | None = None):
        self.dir = Path(root) / "keys"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._master = master_key if master_key is not None else master_key_from_env()
        self._cache: dict[str, bytes] = {}
        # Bumped by every destroy, so holders of a key outside this cache (a DuckDB
        # connection) know to drop theirs.
        self.epoch = 0

    def _path(self, tenant_id: str) -> Path:
        return self.dir / f"{tenant_id}.wrapped"

    def tenant_key(self, tenant_id: str, create: bool = False) -> bytes:
        if tenant_id in self._cache:
            return self._cache[tenant_id]
        p = self._path(tenant_id)
        if not p.exists():
            if not create:
                raise KeyDestroyed(f"no key for tenant {tenant_id}: never created or destroyed")
            key = os.urandom(32)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(aes_key_wrap(self._master, key))
            tmp.replace(p)
        try:
            key = aes_key_unwrap(self._master, p.read_bytes())
        except InvalidUnwrap as e:
            raise KeyError_(f"tenant {tenant_id}: wrapped key does not unwrap under this master key") from e
        self._cache[tenant_id] = key
        return key

    def destroy(self, tenant_id: str) -> bool:
        """Irreversibly destroy the tenant's key. Returns whether a key existed."""
        self._cache.pop(tenant_id, None)
        self.epoch += 1
        p = self._path(tenant_id)
        if not p.exists():
            return False
        size = p.stat().st_size
        with open(p, "r+b") as f:  # overwrite before unlinking, so the bytes are not left behind
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
        return True

    def tenants(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.wrapped"))
