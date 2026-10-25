import base64
import os

import pytest

from matsya_platform.store.keys import MASTER_KEY_ENV


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    """The loop's stores are encrypted and refuse to start without a master key; each test gets
    a fresh random one (spawned sweep workers inherit it through the environment)."""
    monkeypatch.setenv(MASTER_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
