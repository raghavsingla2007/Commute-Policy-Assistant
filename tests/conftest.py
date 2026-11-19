"""Test setup: force FakeLLM and an isolated data dir so tests never touch real keys or real data."""

import pytest


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEV_MODE", "true")
