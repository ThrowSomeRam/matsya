import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from matsya_platform.api import app as api  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "owner_view.json").write_text(json.dumps({"meta": {"tenant_id": "t-1"}, "headline": {}}))
    (tmp_path / "owner_view_t-2.json").write_text(json.dumps({"meta": {"tenant_id": "t-2"}}))
    (tmp_path / "naive_vs_ours.json").write_text(json.dumps({"days": [1, 2]}))
    monkeypatch.setenv("MATSYA_PAYLOAD_DIR", str(tmp_path))
    return TestClient(api.app)


def test_owner_view_by_tenant_and_demo_alias(client):
    assert client.get("/tenants/t-1/owner-view").json()["meta"]["tenant_id"] == "t-1"
    assert client.get("/tenants/t-2/owner-view").json()["meta"]["tenant_id"] == "t-2"
    assert client.get("/tenants/demo/owner-view").json()["meta"]["tenant_id"] == "t-1"
    assert client.get("/tenants/nobody/owner-view").status_code == 404


def test_evidence_and_health(client):
    assert client.get("/evidence/naive-vs-ours").json() == {"days": [1, 2]}
    assert client.get("/health").json()["owner_views"] == 2


def test_missing_evidence_is_404(client, tmp_path):
    (tmp_path / "naive_vs_ours.json").unlink()
    assert client.get("/evidence/naive-vs-ours").status_code == 404


def test_repo_demo_payload_is_served_when_built():
    from pathlib import Path

    demo = Path(api.__file__).resolve().parents[4] / "eval" / "demo" / "owner_view.json"
    if not demo.exists():
        pytest.skip("eval/demo/owner_view.json not built")
    c = TestClient(api.app)
    tenant = json.loads(demo.read_text())["meta"]["tenant_id"]
    assert c.get(f"/tenants/{tenant}/owner-view").status_code == 200
