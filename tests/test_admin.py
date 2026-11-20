"""P6: admin ingest API (roles, own-company rule, errors, size limit, status) and the live v1 -> v2 upgrade
while readers keep asking."""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import messages
from app.auth import mint_token
from app.main import app

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"
ESCORT_Q = "After what time do women get a security escort on night drops?"


def doc(name: str) -> str:
    return (POLICIES / name).read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A fresh app per test: seeded with v1 of all three companies."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def hybrid_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    with TestClient(app) as c:
        assert app.state.settings.retrieval_mode == "hybrid"
        yield c


def auth(company="kestrel", role="admin") -> dict:
    return {"Authorization": f"Bearer {mint_token(app.state.settings, company, role, f'{role}-user')}"}


def upload(client, markdown, company="kestrel", role="admin"):
    return client.post("/admin/policies", headers=auth(company, role), json={"markdown": markdown})


def status(client, company="kestrel"):
    return client.get("/admin/policies/status", headers=auth(company)).json()


# --- roles and the own-company rule -------------------------------------------------------------
def test_employee_cannot_upload_or_read_status(client):
    resp = upload(client, doc("kestrel_v2.md"), role="employee")
    assert resp.status_code == 403 and resp.json()["status"] == "forbidden"
    assert resp.json()["message"] == messages.FORBIDDEN
    assert client.get("/admin/policies/status", headers=auth(role="employee")).status_code == 403
    assert status(client)["active_version"] == 1


def test_admin_of_kestrel_cannot_upload_a_document_claiming_orbitron(client):
    claims_orbitron = doc("kestrel_v2.md").replace("company_id: kestrel", "company_id: orbitron")
    before = (status(client, "kestrel"), status(client, "orbitron"))
    resp = upload(client, claims_orbitron, company="kestrel")
    assert resp.status_code == 403 and resp.json()["message"] == messages.OTHER_COMPANY
    after = (status(client, "kestrel"), status(client, "orbitron"))
    assert [{k: v for k, v in s.items() if k != "request_id"} for s in before] == \
           [{k: v for k, v in s.items() if k != "request_id"} for s in after]            # nothing changed


def test_admin_sees_only_own_company_status(client):
    s = status(client, "tamarind")
    assert (s["company_id"], s["active_version"], s["doc_id"], s["chunk_count"]) == \
           ("tamarind", 1, "tamarind-commute-policy", 11)
    assert s["versions"] == [{"version": 1, "status": "active"}]


def test_no_token_is_401(client):
    assert client.post("/admin/policies", json={"markdown": "x"}).status_code == 401


# --- errors: active policy untouched -------------------------------------------------------------
def test_invalid_document_422(client):
    resp = upload(client, "# Not a policy\n\nno header, no sections")
    assert resp.status_code == 422 and resp.json() == {"status": "invalid_policy", "message": messages.INVALID_POLICY,
                                                       "request_id": resp.json()["request_id"]}
    assert status(client)["active_version"] == 1


def test_huge_version_is_422_not_503(client):
    """P10 audit: version 2**70 overflowed SQLite's INTEGER and surfaced as the catch-all 503."""
    resp = upload(client, doc("kestrel_v2.md").replace("version: 2", f"version: {2**70}"))
    assert resp.status_code == 422 and resp.json()["status"] == "invalid_policy"
    assert status(client)["active_version"] == 1


@pytest.mark.parametrize("body", [{"markdown": 5}, {"markdown": "x", "version": 2}, {"text": "x"}])
def test_bad_body_422(client, body):
    resp = client.post("/admin/policies", headers=auth(), json=body)
    assert resp.status_code == 422 and resp.json()["message"] == messages.BAD_REQUEST


def test_same_or_older_version_409_active_unchanged(client):
    resp = upload(client, doc("kestrel_v1.md"))
    assert resp.status_code == 409 and resp.json()["message"] == messages.VERSION_CONFLICT
    assert status(client)["active_version"] == 1


def test_ingest_failure_500_says_policy_unchanged(client):
    store = app.state.store
    real = store.embedder
    calls = {"n": 0}

    def failing(texts):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("embedder died")
        return real(texts)

    store.embedder = failing
    try:
        resp = upload(client, doc("kestrel_v2.md"))
    finally:
        store.embedder = real
    assert resp.status_code == 500 and resp.json()["status"] == "ingest_failed"
    assert resp.json()["message"] == messages.INGEST_FAILED and "unchanged" in resp.json()["message"]
    assert "embedder died" not in resp.text
    s = status(client)
    assert s["active_version"] == 1 and {"version": 2, "status": "failed"} in s["versions"]


def test_413_over_admin_limit(client):
    resp = upload(client, "x" * (300 * 1024))
    assert resp.status_code == 413 and resp.json()["status"] == "payload_too_large"
    assert status(client)["active_version"] == 1


# --- happy path: the live upgrade ---------------------------------------------------------------
def test_upgrade_then_answers_come_from_v2(client):
    resp = upload(client, doc("kestrel_v2.md"))
    body = resp.json()
    assert resp.status_code == 201
    assert {k: body[k] for k in ("status", "company_id", "doc_id", "version", "chunks", "previous_version")} == \
           {"status": "active", "company_id": "kestrel", "doc_id": "kestrel-transport-policy", "version": 2,
            "chunks": 10, "previous_version": 1}
    s = status(client)
    assert (s["active_version"], s["chunk_count"]) == (2, 10)
    assert s["versions"] == [{"version": 1, "status": "retired"}, {"version": 2, "status": "active"}]
    ans = client.post("/ask", headers=auth(role="employee"), json={"question": ESCORT_Q}).json()
    assert ans["policy_version"] == 2 and "9:00 PM" in ans["answer"] and "10:00 PM" not in ans["answer"]
    assert {c["version"] for c in ans["citations"]} == {2}


def test_readers_during_the_flip_see_v1_or_v2_never_a_mix(client):
    readers_during_flip(client)


def test_readers_during_the_flip_hybrid_mode(hybrid_client):
    """(3) The same live-upgrade-under-load test with BM25 + vector retrieval."""
    readers_during_flip(hybrid_client)


def readers_during_flip(client):
    """4 readers ask continuously while the admin upgrades Kestrel v1 -> v2.
    Each answer must be wholly v1 or wholly v2. The only other allowed outcome is the documented 503."""
    employee = auth(role="employee")
    results, stop = [], threading.Event()
    lock = threading.Lock()

    def reader():
        while not stop.is_set():
            r = client.post("/ask", headers=employee, json={"question": ESCORT_Q})
            with lock:
                results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    up = upload(client, doc("kestrel_v2.md"))
    time.sleep(0.3)
    stop.set()
    for t in threads:
        t.join()

    assert up.status_code == 201
    codes = {code for code, _ in results}
    assert codes <= {200, 503}, codes
    versions_seen = set()
    for code, body in results:
        if code == 503:
            assert body["status"] == "unavailable" and body["answer"] == messages.UNAVAILABLE
            continue
        v = body["policy_version"]
        versions_seen.add(v)
        assert {c["version"] for c in body["citations"]} == {v}                     # no mixed citations
        expected, stale = ("10:00 PM", "9:00 PM") if v == 1 else ("9:00 PM", "10:00 PM")
        assert expected in body["answer"] and stale not in body["answer"]         # text from that version only
    assert versions_seen == {1, 2}, versions_seen                                   # the flip happened mid-stream
    print(f"\nreaders: {len(results)} responses, 503s: {sum(c == 503 for c, _ in results)}")
    final = client.post("/ask", headers=employee, json={"question": ESCORT_Q}).json()
    assert final["policy_version"] == 2
