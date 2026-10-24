"""Deterministic RNG derived from run_id. Never use global RNG state."""

import hashlib

import numpy as np


def seed_from_run_id(run_id: str, stream: str = "") -> int:
    digest = hashlib.sha256(f"{run_id}:{stream}".encode()).digest()
    return int.from_bytes(digest[:8], "little")


def rng_for(run_id: str, stream: str = "") -> np.random.Generator:
    return np.random.default_rng(seed_from_run_id(run_id, stream))
