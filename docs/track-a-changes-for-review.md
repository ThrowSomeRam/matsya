# Track A changes for review (owner view)

Made from Track B to run the owner view end to end, with the owner's go-ahead. Each one is the minimum
needed. No existing Track A code was refactored or rewritten. Please review; revert or move anything that
belongs elsewhere.

## 1. Owner-view API (new)

`platform/src/matsya_platform/api/app.py` (new package `matsya_platform.api`). No serving layer existed, so
it sits in the platform package beside the other Track A components.

- `GET /tenants/{tenant_id}/owner-view`: the OwnerView payload. Track B builds it
  (`intelligence/src/matsya_intelligence/present/owner_view.py`).
- `GET /evidence/naive-vs-ours`: the chart data from `eval/models/naive_vs_ours.py`.
- `GET /health`.

It serves precomputed JSON files. No simulation-engine or decision-engine run happens on a request.

- **Where the files live:** `MATSYA_PAYLOAD_DIR`, default `eval/demo`.
- **Files:** `owner_view.json` (or `owner_view_<tenant_id>.json`) and `naive_vs_ours.json`.
- **Tenant lookup:** a tenant is served when a file's `meta.tenant_id` matches. `demo` is an alias for
  `owner_view.json`.
- **Reload:** files are re-read when their mtime changes.
- **Access:** read-only (GET only). CORS is open by default (`MATSYA_CORS_ORIGINS` narrows it).

Why: the owner screen needs one HTTP source. Serving precomputed payloads was agreed for the demo.

Open for Track A:

- **Auth and tenant isolation.** There is none: any caller can read any tenant's payload. That is fine on
  127.0.0.1 with synthetic data. It must change before any real tenant data goes in.
- **Where payloads come from.** They should come from the pipeline's outputs, not files in the repository.

Tests: `platform/tests/test_api.py` (4 tests: tenant and demo lookup, 404s, health, the repository's demo
payload). They are skipped when FastAPI is not installed.

## 2. Packaging

- `platform/pyproject.toml`: new optional extra `api = ["fastapi>=0.110", "uvicorn>=0.29", "httpx>=0.27"]`.
  httpx is only there for FastAPI's test client.
- `Makefile`, `setup` target: installs `platform[dev,s3,api]`, so `make test` runs the API tests.

## 3. Compose

`infra/compose.platform.yml`: two new services.

- **`api`**: built from `infra/api.Dockerfile`. Python 3.12-slim with only fastapi and uvicorn, plus the
  `matsya_platform.api` module. It mounts `eval/demo` read-only at `/payloads` and listens on
  127.0.0.1:8000, with a health check on `/health`.
- **`dashboard`**: built from `dashboard/owner/Dockerfile`, with the repository root as context. A Vite
  build runs in node:22-slim and the result is served by nginx:1.27-alpine. nginx proxies `/api/` to
  `api:8000`. It listens on 127.0.0.1:8080 and waits for the API's health check.

It went into the platform compose file because that is where Track A's services run. `make owner-view-up`
brings both services up on the demo world (`make owner-view-down` stops them). Like the other targets it
needs `.env` (`make env`), because the file interpolates the MinIO variables.

Also added: a repository-root `.dockerignore` (node_modules, .venv, dist, .git), to keep the build
context small.

## 4. Contracts

No change. The owner view does not need a new contract table: the API serves Track B's precomputed
payload, which is not a contract table.

The additive proposals already listed in `docs/track-b-decisions.md` are still open:

- `decisions.plans` `amount_paise` / `params_json` / `net_cost_paise` (questions 15-16);
- `decisions.drivers` `horizon_days` / `delta_es_paise`;
- `forecast.scenario_effects` (question 14).

Once the payload is built from stored tables rather than in one process, these are the columns it would
need.

The fixtures and conformance tests are unchanged, and still pass.

## Verified

`make owner-view-up` was checked in this environment with `docker compose up -d --build --wait api dashboard`
on `infra/compose.platform.yml`. Both containers came up healthy.

- **Payloads:** `/api/tenants/demo/owner-view` and `/api/evidence/naive-vs-ours` returned 200 through nginx.
- **Screen:** every element rendered from the API (screenshots in `eval/demo/screenshots/`).
- **Sandbox-only override:** the build in this sandbox needed its proxy CA, added through a local,
  uncommitted compose override. The committed Dockerfiles have no such step.
- **Bug found and fixed:** the API's default payload path assumed the repository layout and crashed
  inside the image. It is now resolved only when `MATSYA_PAYLOAD_DIR` is unset.

## Files

| file | change |
|---|---|
| `platform/src/matsya_platform/api/__init__.py`, `api/app.py` | new |
| `platform/tests/test_api.py` | new |
| `platform/pyproject.toml` | `api` extra |
| `Makefile` | `api` extra in setup; `owner-view-up` / `owner-view-down` |
| `infra/compose.platform.yml` | `api` and `dashboard` services |
| `infra/api.Dockerfile` | new |
| `.dockerignore` | new |
