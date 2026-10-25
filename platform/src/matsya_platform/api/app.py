"""Owner-view API: serves payloads Track B precomputed; no engine or decision runs on request.

    GET /tenants/{tenant_id}/owner-view  -> the OwnerView payload (intelligence present.owner_view)
    GET /evidence/naive-vs-ours          -> the naive vs ours chart data (eval/models/naive_vs_ours.py)
    GET /health

Payloads are JSON files in MATSYA_PAYLOAD_DIR (default eval/demo in the repository):
owner_view.json (or owner_view_<tenant_id>.json) and naive_vs_ours.json. A tenant is served when a
file's meta.tenant_id matches; "demo" is an alias for owner_view.json. Files are re-read when they change.

    uvicorn matsya_platform.api.app:app --port 8000
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

DEMO_ALIAS = "demo"

app = FastAPI(title="matsya owner view", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("MATSYA_CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET"], allow_headers=["*"])

_cache: dict[Path, tuple[float, dict]] = {}


def payload_dir() -> Path:
    """MATSYA_PAYLOAD_DIR, else eval/demo in the repository this file sits in."""
    if "MATSYA_PAYLOAD_DIR" in os.environ:
        return Path(os.environ["MATSYA_PAYLOAD_DIR"])
    return Path(__file__).resolve().parents[4] / "eval" / "demo"


def _read(path: Path) -> dict:
    mtime = path.stat().st_mtime
    hit = _cache.get(path)
    if hit is None or hit[0] != mtime:
        hit = (mtime, json.loads(path.read_text()))
        _cache[path] = hit
    return hit[1]


def _owner_view_files() -> list[Path]:
    d = payload_dir()
    tenants = [p for p in sorted(d.glob("owner_view_*.json")) if p.name != "owner_view_run.json"]  # run record
    return tenants + ([d / "owner_view.json"] if (d / "owner_view.json").exists() else [])


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "payload_dir": str(payload_dir()), "owner_views": len(_owner_view_files())}


@app.get("/tenants/{tenant_id}/owner-view")
def owner_view(tenant_id: str) -> dict:
    d = payload_dir()
    if tenant_id == DEMO_ALIAS and (d / "owner_view.json").exists():
        return _read(d / "owner_view.json")
    for path in _owner_view_files():
        p = _read(path)
        if p.get("meta", {}).get("tenant_id") == tenant_id:
            return p
    raise HTTPException(status_code=404, detail=f"no owner view for tenant {tenant_id}")


@app.get("/evidence/naive-vs-ours")
def naive_vs_ours() -> dict:
    path = payload_dir() / "naive_vs_ours.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="naive vs ours data not built")
    return _read(path)
