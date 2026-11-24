"""Sandbox for the core tests.

``tokenmana.constants`` reads its paths once, at import time, and other
modules copy them, so the config, cache and home directories are pointed at a
temporary directory here, before any test module imports ``tokenmana``. The
local time zone is fixed (Europe/Paris, which changes for DST) so scheduled
times give the same results on every machine.

Use ``tokenmana.constants`` (``HOME``, ``CONFIG_PATH``, ``STATE_PATH``...) for
the sandboxed locations.
"""
import os
import shutil
import sys
import tempfile
import time

import pytest

if "tokenmana" in sys.modules:
    raise RuntimeError("tokenmana was imported before the test sandbox was set up")

SANDBOX = os.path.realpath(tempfile.mkdtemp(prefix="tokenmana-tests-"))
DIRS = [os.path.join(SANDBOX, d) for d in ("home", "config", "cache")]
os.environ.update(HOME=DIRS[0], TOKENMANA_CONFIG_DIR=DIRS[1], TOKENMANA_CACHE_DIR=DIRS[2], TZ="Europe/Paris")
for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(var, None)
time.tzset()


def pytest_unconfigure(config):
    shutil.rmtree(SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def sandbox(monkeypatch):
    """Start every test with empty home, config and cache directories, and no keychain."""
    from tokenmana import config
    from tokenmana.providers import claude

    for d in DIRS:
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    # Never read the developer's real keychain (it can also pop up an access prompt).
    monkeypatch.setattr(config, "keychain_read", lambda service: None)
    monkeypatch.setattr(claude, "keychain_read", lambda service: None)
    return SANDBOX
