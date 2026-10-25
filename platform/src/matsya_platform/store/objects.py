"""Object storage under the raw and table stores: a local directory, or an S3 bucket (MinIO).

Both hold only ciphertext: raw payloads are AES-GCM blobs and table parts are encrypted
Parquet, so the backend is never trusted with plaintext. Keys never live here; KeyStore keeps
them on local disk. Each backend can also hand DuckDB a URL for an object and configure a
connection to read it, so queries read the encrypted parts in place.

The S3 backend refuses a bucket with versioning (enabled or suspended) or object lock: either
keeps old object versions after a delete, and an old version of a tenant's data is exactly what
erasure must not leave behind. The bucket, its credentials and endpoint come from the
environment (MATSYA_S3_*; see .env.example), never from code.

open_objects(root) picks the backend: S3 when MATSYA_S3_ENDPOINT is set, else <root>/ on disk.
Under S3 each store root gets its own prefix, so two stores sharing a bucket never see each
other's objects.
"""

import hashlib
import os
import shutil
from pathlib import Path
from typing import Protocol

import duckdb

S3_ENDPOINT_ENV = "MATSYA_S3_ENDPOINT"
S3_BUCKET_ENV = "MATSYA_S3_BUCKET"
S3_ACCESS_KEY_ENV = "MATSYA_S3_ACCESS_KEY"
S3_SECRET_KEY_ENV = "MATSYA_S3_SECRET_KEY"
S3_PREFIX_ENV = "MATSYA_S3_PREFIX"


class StoreConfigError(RuntimeError):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> bool: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def url(self, key: str) -> str: ...
    def configure(self, con: duckdb.DuckDBPyConnection) -> None: ...
    def clear(self) -> None: ...


class LocalObjects:
    """Objects as files under root. A put writes a temporary file and renames it, so a reader
    never sees a partial object."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._abs = self.root.resolve()

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        p.unlink()
        return True

    def list_keys(self, prefix: str) -> list[str]:
        root, out = str(self.root), []
        for d, _, names in os.walk(self._path(prefix)):
            rel = os.path.relpath(d, root)
            out.extend(f"{rel}/{n}" for n in names if not n.startswith("."))
        return sorted(out)

    def url(self, key: str) -> str:
        return str(self._abs / key)

    def configure(self, con: duckdb.DuckDBPyConnection) -> None:
        pass

    def clear(self) -> None:
        for d in ("raw", "tables"):
            shutil.rmtree(self.root / d, ignore_errors=True)


class S3Objects:
    """Objects in an S3 bucket under a prefix. S3 PUTs are atomic, so no temporary objects."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, prefix: str = "",
                 region: str = "us-east-1"):
        import boto3  # optional dependency: the "s3" extra

        self.endpoint, self.bucket, self.prefix = endpoint.rstrip("/"), bucket, prefix.strip("/")
        self._access, self._secret, self._region = access_key, secret_key, region
        self.client = boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=access_key,
                                   aws_secret_access_key=secret_key, region_name=region)
        self._refuse_retaining_bucket()

    def _refuse_retaining_bucket(self) -> None:
        status = self.client.get_bucket_versioning(Bucket=self.bucket).get("Status")
        if status in ("Enabled", "Suspended"):
            raise StoreConfigError(f"bucket {self.bucket} has versioning {status}: deleted objects would keep old "
                                   "versions, which defeats erasure. Use a bucket that never had versioning.")
        try:
            lock = self.client.get_object_lock_configuration(Bucket=self.bucket)
        except self.client.exceptions.ClientError as e:
            if e.response["Error"]["Code"] not in ("ObjectLockConfigurationNotFoundError", "NoSuchObjectLockConfiguration"):
                raise
        else:
            if lock.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled":
                raise StoreConfigError(f"bucket {self.bucket} has object lock: objects could not be deleted on erasure")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.client.exceptions.NoSuchKey as e:
            raise FileNotFoundError(key) from e

    def delete(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        return True

    def list_keys(self, prefix: str) -> list[str]:
        out, cut = [], len(self.prefix) + 1 if self.prefix else 0
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            out.extend(o["Key"][cut:] for o in page.get("Contents", []))
        return sorted(out)

    def versions(self, prefix: str = "") -> list[str]:
        """Every stored version and delete marker under prefix. With versioning never enabled
        this equals list_keys(); anything extra would be data a delete did not remove."""
        out, cut = [], len(self.prefix) + 1 if self.prefix else 0
        for page in self.client.get_paginator("list_object_versions").paginate(Bucket=self.bucket,
                                                                              Prefix=self._key(prefix)):
            out.extend(v["Key"][cut:] for v in page.get("Versions", []) + page.get("DeleteMarkers", []))
        return sorted(out)

    def url(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


    def configure(self, con: duckdb.DuckDBPyConnection) -> None:
        host = self.endpoint.split("://", 1)[-1]
        con.execute("LOAD httpfs")
        con.execute("CREATE OR REPLACE TEMPORARY SECRET matsya_s3 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, "
                    "USE_SSL ?, URL_STYLE 'path', REGION ?, SCOPE ?)",
                    [self._access, self._secret, host, self.endpoint.startswith("https://"), self._region,
                     f"s3://{self.bucket}"])

    def clear(self) -> None:
        keys = self.list_keys("")
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={
                "Objects": [{"Key": self._key(k)} for k in keys[i:i + 1000]], "Quiet": True})


def open_objects(root: str | Path) -> ObjectStore:
    endpoint = os.environ.get(S3_ENDPOINT_ENV)
    if not endpoint:
        return LocalObjects(root)
    missing = [v for v in (S3_BUCKET_ENV, S3_ACCESS_KEY_ENV, S3_SECRET_KEY_ENV) if not os.environ.get(v)]
    if missing:
        raise StoreConfigError(f"{S3_ENDPOINT_ENV} is set but {', '.join(missing)} is not (see .env.example)")
    resolved = Path(root).resolve()
    store = f"{resolved.name}-{hashlib.sha256(str(resolved).encode()).hexdigest()[:12]}"
    prefix = "/".join(p for p in (os.environ.get(S3_PREFIX_ENV, "").strip("/"), store) if p)
    return S3Objects(endpoint, os.environ[S3_BUCKET_ENV], os.environ[S3_ACCESS_KEY_ENV],
                     os.environ[S3_SECRET_KEY_ENV], prefix=prefix)
