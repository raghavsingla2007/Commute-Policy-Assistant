"""Test setup: force FakeLLM and an isolated data dir so tests never touch real keys or real data.

Real-LLM tests are marked `groq` and skipped unless RUN_GROQ_TESTS=1 and a key is present, so the
suite (including `--network none`) is green with no key.
"""

import os

import pytest

# Captured before the autouse fixture removes it; only `groq`-marked tests may use it.
REAL_GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
RUN_GROQ_TESTS = os.environ.get("RUN_GROQ_TESTS") == "1"


def pytest_configure(config):
    config.addinivalue_line("markers", "groq: calls the real Groq API (skipped unless RUN_GROQ_TESTS=1 and a key is set)")


def pytest_collection_modifyitems(config, items):
    if RUN_GROQ_TESTS and REAL_GROQ_KEY:
        return
    skip = pytest.mark.skip(reason="real Groq test: set RUN_GROQ_TESTS=1 and GROQ_API_KEY to run")
    for item in items:
        if "groq" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEV_MODE", "true")
