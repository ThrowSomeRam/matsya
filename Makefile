# Track A: set up, test, and run MinIO.
#
#   make setup          venv (.venv) with contracts, platform and eval installed editable
#   make env            write .env with a fresh random master key and MinIO keys (only if missing)
#   make test           contracts, platform and eval suites, local object store
#   make test-pipeline  just the pipeline tests (eval/platform/tests/test_pipeline.py)
#   make test-s3        platform and eval suites against MinIO (starts it)
#   make minio-up       MinIO on 127.0.0.1:9000, console on http://127.0.0.1:9001
#   make minio-down     stop it, keep the data;  make minio-reset  stop it and delete the data
#   make owner-view-up  owner-view API and dashboard on the demo world: http://127.0.0.1:8080
#   make owner-view-down
#
# .env is gitignored and holds real secrets: never commit it.

SHELL := /bin/bash
VENV := .venv
PY := $(CURDIR)/$(VENV)/bin/python
PYTEST := $(PY) -m pytest -q -p no:cacheprovider
COMPOSE := docker compose -p matsya --env-file .env -f infra/compose.platform.yml

-include .env
export

.PHONY: setup env test test-contracts test-platform test-eval test-pipeline test-s3 \
        minio-up minio-down minio-reset owner-view-up owner-view-down

setup: $(VENV)/.installed

$(VENV)/.installed: contracts/pyproject.toml platform/pyproject.toml eval/platform/pyproject.toml
	uv venv --allow-existing --python 3.12 $(VENV)
	uv pip install --python $(PY) -e contracts[dev] -e 'platform[dev,s3,api]' -e eval/platform[dev]
	touch $@

env:
	@if [ -f .env ]; then echo ".env exists, leaving it"; else \
	  rand() { python3 -c "import os, base64; print(base64.b64encode(os.urandom($$1)).decode())"; }; \
	  { echo "MATSYA_MASTER_KEY=$$(rand 32)"; \
	    echo "MATSYA_S3_BUCKET=matsya"; \
	    echo "MATSYA_S3_ACCESS_KEY=matsya-dev"; \
	    echo "MATSYA_S3_SECRET_KEY=$$(rand 24 | tr -d '/+=')"; } > .env; \
	  chmod 600 .env; echo "wrote .env"; fi

test: test-contracts test-platform test-eval

test-contracts: setup
	cd contracts && $(PYTEST)

# MATSYA_S3_ENDPOINT is cleared so these run on the local object store even if .env sets it.
test-platform: setup
	cd platform && MATSYA_S3_ENDPOINT= $(PYTEST)

test-eval: setup
	cd eval/platform && MATSYA_S3_ENDPOINT= $(PYTEST)

test-pipeline: setup
	cd eval/platform && MATSYA_S3_ENDPOINT= $(PYTEST) tests/test_pipeline.py -v

# The platform store tests run their s3 cases from MATSYA_TEST_S3_*; the eval suite runs whole
# on MinIO through MATSYA_S3_*, under its own prefix.
test-s3: setup minio-up
	cd platform && MATSYA_TEST_S3_ENDPOINT=http://127.0.0.1:9000 MATSYA_TEST_S3_BUCKET=$(MATSYA_S3_BUCKET) \
	  MATSYA_TEST_S3_ACCESS_KEY=$(MATSYA_S3_ACCESS_KEY) MATSYA_TEST_S3_SECRET_KEY=$(MATSYA_S3_SECRET_KEY) \
	  MATSYA_S3_ENDPOINT= $(PYTEST)
	cd eval/platform && MATSYA_S3_ENDPOINT=http://127.0.0.1:9000 MATSYA_S3_PREFIX=evaltest $(PYTEST)

minio-up:
	@[ -f .env ] || { echo "no .env: run make env first"; exit 1; }
	$(COMPOSE) up -d --wait minio
	$(COMPOSE) run --rm minio-init

minio-down:
	$(COMPOSE) down

minio-reset:
	$(COMPOSE) down -v

owner-view-up:
	@[ -f .env ] || { echo "no .env: run make env first"; exit 1; }
	$(COMPOSE) up -d --build --wait api dashboard

owner-view-down:
	$(COMPOSE) stop api dashboard
