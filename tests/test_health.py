"""P0 smoke test: the app starts, warms up, and reports its mode without any API key."""

import pytest
from fastapi.testclient import TestClient

from app.config import DEV_DEFAULT_JWT_SECRET, load_settings
from app.main import app


def test_health_ready_with_fake_llm():
    with TestClient(app) as client:  # `with` runs the startup warm-up
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "ready": True, "llm": "fake", "dev_mode": True}


def test_short_jwt_secret_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValueError, match="at least"):
        load_settings().validate()


def test_dev_default_secret_rejected_when_dev_mode_off(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JWT_SECRET", DEV_DEFAULT_JWT_SECRET)
    with pytest.raises(ValueError, match="dev default"):
        load_settings().validate()


@pytest.mark.parametrize("k", ["0", "4"])
def test_retrieval_k_outside_1_to_3_is_refused(monkeypatch, k):
    """The 3-cited-chunks cap starts here: the app refuses to start with RETRIEVAL_K outside 1..3."""
    monkeypatch.setenv("RETRIEVAL_K", k)
    with pytest.raises(ValueError, match="between 1 and 3"):
        load_settings().validate()
