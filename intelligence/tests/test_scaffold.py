import importlib

import pytest

from matsya_intelligence.seeding import rng_for, seed_from_run_id

MODULES = ["categorise", "obligations", "priors", "timing", "flows",
           "engine", "calibrate", "shocks", "decide", "attribute"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(f"matsya_intelligence.{name}")


def test_seed_is_deterministic():
    assert seed_from_run_id("run-1") == seed_from_run_id("run-1")
    assert seed_from_run_id("run-1") != seed_from_run_id("run-2")
    assert seed_from_run_id("run-1", "engine") != seed_from_run_id("run-1", "decide")


def test_rng_streams_bit_identical():
    a = rng_for("run-1", "engine").standard_normal(100)
    b = rng_for("run-1", "engine").standard_normal(100)
    assert (a == b).all()
