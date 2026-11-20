"""P6: POST /ask over HTTP: response shape, input validation, error mapping (401/413/422/503), request ids,
no internals in error bodies, per-request LLM budget under concurrency. All offline (FakeLLM)."""

import dataclasses
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import messages, retrieval
from app.auth import mint_token
from app.llm import FakeLLM, LLMUnavailable
from app.main import app
from app.retrieval import Hits, RetrievedChunk

ASK_KEYS = {"status", "answer", "citations", "company_id", "policy_version", "request_id"}
ERROR_KEYS = {"status", "message", "request_id"}
ESCORT_Q = "After what time do women get a security escort on night drops?"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LLM_PROVIDER", "fake")
        mp.delenv("GROQ_API_KEY", raising=False)
        mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("api") / "data"))
        mp.setenv("DEV_MODE", "true")
        with TestClient(app) as c:
            yield c


@pytest.fixture
def llm(client):
    """Swap the app's LLM for one test, then restore it."""
    original = app.state.llm

    def use(new):
        app.state.llm = new
        return new

    yield use
    app.state.llm = original


@pytest.fixture
def settings_override(client):
    original = app.state.settings

    def use(**changes):
        app.state.settings = dataclasses.replace(original, **changes)

    yield use
    app.state.settings = original


def token(company="kestrel", role="employee") -> str:
    return mint_token(app.state.settings, company, role, "tester")


def ask(client, question=ESCORT_Q, company="kestrel", **kw):
    headers = {"Authorization": f"Bearer {token(company)}", **kw.pop("headers", {})}
    if "content" in kw or "json" in kw:
        return client.post("/ask", headers=headers, **kw)
    return client.post("/ask", headers=headers, json={"question": question})


class Scripted:
    name = "scripted"

    def __init__(self, reply=None, sleep=0.0, error=None):
        self.reply, self.sleep, self.error, self.calls = reply, sleep, error, 0

    def complete(self, prompt):
        self.calls += 1
        time.sleep(self.sleep)
        if self.error:
            raise self.error
        return json.dumps(self.reply)


# --- 200 answered / not_covered -------------------------------------------------------------
def test_answered_shape(client):
    resp = ask(client)
    body = resp.json()
    assert resp.status_code == 200 and set(body) == ASK_KEYS
    assert (body["status"], body["company_id"], body["policy_version"]) == ("answered", "kestrel", 1)
    assert "10:00 PM" in body["answer"]                                    # Kestrel v1 is active after seeding
    [c] = body["citations"]
    assert set(c) == {"doc_id", "version", "section_id", "section_title", "snippet"}
    assert (c["doc_id"], c["version"], c["section_id"]) == ("kestrel-transport-policy", 1, "4")
    assert resp.headers["x-request-id"] == body["request_id"]


def test_not_covered_shape_hides_reason(client, llm):
    llm(Scripted({"status": "not_covered", "answer": "internal musings", "citations": []}))
    for question in ["What was the cricket score yesterday?", "Can I take my dog in the cab?"]:  # threshold / LLM
        resp = ask(client, question)
        body = resp.json()
        assert resp.status_code == 200 and set(body) == ASK_KEYS
        assert (body["status"], body["answer"], body["citations"]) == ("not_covered", messages.NOT_COVERED, [])
        assert "reason" not in resp.text and "below_threshold" not in resp.text and "musings" not in resp.text


# --- 401 --------------------------------------------------------------------------------------
def test_401_same_body_and_checked_before_the_body(client):
    bodies = set()
    for headers, payload in [({}, {"question": "q"}), ({"Authorization": "Bearer not.a.jwt"}, {"question": "q"}),
                             ({"Authorization": "Bearer not.a.jwt"}, {"bad": 1})]:
        resp = client.post("/ask", headers=headers, json=payload)
        assert resp.status_code == 401
        body = resp.json()
        assert set(body) == ERROR_KEYS
        bodies.add((body["status"], body["message"]))
    assert bodies == {("auth_error", messages.AUTH_ERROR)}


# --- 422 --------------------------------------------------------------------------------------
BAD_INPUTS = {
    "empty": {"json": {"question": ""}},
    "whitespace": {"json": {"question": "   \n\t "}},
    "10k_chars": {"json": {"question": "a" * 10_000}},
    "501_chars": {"json": {"question": "a" * 501}},
    "number": {"json": {"question": 42}},
    "null": {"json": {"question": None}},
    "list": {"json": {"question": ["q"]}},
    "missing": {"json": {}},
    "company_in_body": {"json": {"question": "escort?", "company_id": "orbitron"}},
    "extra_field": {"json": {"question": "escort?", "debug": True}},
    "null_byte": {"json": {"question": "escort\u0000?"}},
    "bell_char": {"json": {"question": "escort\u0007?"}},
    "non_json": {"content": b"question=escort", "headers": {"content-type": "application/json"}},
    "wrong_content_type": {"content": b"escort?", "headers": {"content-type": "text/plain"}},
    "json_array": {"json": ["escort?"]},
}


@pytest.mark.parametrize("name", sorted(BAD_INPUTS))
def test_422_plain_message_no_echo(client, name):
    kw = dict(BAD_INPUTS[name])
    tok = token()
    resp = client.post("/ask", headers={"Authorization": f"Bearer {tok}", **kw.pop("headers", {})}, **kw)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert set(body) == ERROR_KEYS and body["status"] == "bad_request" and body["message"] == messages.BAD_QUESTION
    assert tok not in resp.text and "orbitron" not in resp.text and "aaaa" not in resp.text
    assert "loc" not in resp.text and "input" not in resp.text            # FastAPI's default echo is gone


def test_question_is_stripped_and_tabs_newlines_allowed(client):
    resp = ask(client, "  After what time\tdo women get an escort?\n ")
    assert resp.status_code == 200 and resp.json()["status"] == "answered"


# --- 413 --------------------------------------------------------------------------------------
def test_413_declared_length(client):
    big = json.dumps({"question": "a" * 20_000}).encode()
    resp = ask(client, content=big, headers={"content-type": "application/json"})
    assert resp.status_code == 413 and resp.json()["status"] == "payload_too_large"
    assert set(resp.json()) == ERROR_KEYS and "x-request-id" in resp.headers


def test_413_streamed_body_without_length(client):
    def chunks():
        yield b'{"question": "'
        for _ in range(40):
            yield b"a" * 1024
        yield b'"}'

    resp = ask(client, content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413 and resp.json()["message"] == messages.PAYLOAD_TOO_LARGE


# --- 503 mapping -------------------------------------------------------------------------------
class ForcedRetriever:
    name = "vector"

    def __init__(self, chunks=None, sleep_s=0.0):
        self.chunks, self.sleep_s = chunks or [], sleep_s

    def search(self, store, active, question, k):
        time.sleep(self.sleep_s)
        return Hits.of(self.chunks)


FOREIGN = RetrievedChunk("orbitron:v1:s4:p1", "Section 4 Night\nAll employees after 11:00 PM. Nimbus Rides.", "orbitron",
                         "orbitron-transport-guidelines", 1, "4", "Night Shift Safety", 0.9)


def assert_503(resp, retry_after="2"):
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert set(body) == ASK_KEYS
    assert (body["status"], body["answer"], body["citations"]) == ("unavailable", messages.UNAVAILABLE, [])
    assert resp.headers["retry-after"] == retry_after
    for leak in ("Traceback", "Error", "timeout", "rate", "isolation", "Nimbus", "orbitron", "malformed", "boom"):
        assert leak not in resp.text, leak


def test_503_llm_timeout(client, llm):
    llm(FakeLLM("timeout"))
    assert_503(ask(client))


def test_503_llm_429_passes_retry_after(client, llm):
    llm(Scripted(error=LLMUnavailable("rate_limited", retry_after=7.2)))
    assert_503(ask(client), retry_after="8")


def test_503_malformed_output(client, llm):
    llm(FakeLLM("malformed"))
    assert_503(ask(client))


def test_503_slow_retrieval(client, monkeypatch):
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", ForcedRetriever(sleep_s=1.0))
    started = time.perf_counter()
    assert_503(ask(client))
    assert time.perf_counter() - started < 1.0                          # answered at the 0.5 s retrieval limit


def test_503_forced_isolation_violation(client, monkeypatch, llm):
    spy = llm(Scripted({"status": "answered", "answer": "x", "citations": ["S1"]}))
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", ForcedRetriever([FOREIGN]))
    assert_503(ask(client))
    assert spy.calls == 0                                               # the foreign chunk never reached the model


def test_503_overall_deadline(client, llm, settings_override):
    settings_override(request_timeout_s=0.3)
    llm(Scripted({"status": "answered", "answer": "Escort after 10:00 PM.", "citations": ["S1"]}, sleep=1.0))
    started = time.perf_counter()
    assert_503(ask(client))
    assert time.perf_counter() - started < 0.9


def test_503_unhandled_exception_logs_traceback_with_request_id(client, llm, caplog):
    llm(Scripted(error=RuntimeError("boom secret detail")))
    with caplog.at_level(logging.ERROR, logger="app"):
        resp = ask(client)
    assert_503(resp)
    assert "secret detail" not in resp.text
    [record] = [r for r in caplog.records if "cause=unhandled" in r.getMessage()]
    assert resp.json()["request_id"] in record.getMessage() and record.exc_info is not None


def test_503_from_middleware_catch_all(client):
    """An exception outside /ask's own handling (here in /health) still becomes a plain 503.
    (Saved/restored by hand: pytest's monkeypatch can't restore attributes on Starlette's State.)"""
    saved = app.state.settings
    app.state.settings = None                                          # /health will crash on None.llm_mode
    try:
        resp = client.get("/health")
    finally:
        app.state.settings = saved
    assert resp.status_code == 503 and resp.json()["status"] == "unavailable"
    assert resp.json()["message"] == messages.UNAVAILABLE and "llm_mode" not in resp.text
    assert resp.headers["retry-after"] == "2" and resp.headers["x-request-id"] == resp.json()["request_id"]


# --- request ids -------------------------------------------------------------------------------
def test_request_id_generated_when_absent(client):
    resp = ask(client)
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])


def test_safe_client_request_id_is_echoed(client):
    resp = ask(client, headers={"X-Request-ID": "demo-42.a_b"})
    assert resp.headers["x-request-id"] == "demo-42.a_b" == resp.json()["request_id"]


@pytest.mark.parametrize("bad", ["<script>alert(1)</script>", "a" * 65, "id with spaces", "x;y", "a/b"])
def test_unsafe_client_request_id_is_replaced(client, bad):
    resp = ask(client, headers={"X-Request-ID": bad})
    rid = resp.headers["x-request-id"]
    assert rid != bad and re.fullmatch(r"[0-9a-f]{32}", rid) and rid == resp.json()["request_id"]


@pytest.mark.parametrize("call", ["401", "422", "404", "413"])
def test_request_id_on_every_response(client, call):
    if call == "401":
        resp = client.post("/ask", json={"question": "q"})
    elif call == "422":
        resp = ask(client, json={"question": ""})
    elif call == "404":
        resp = client.get("/nope")
    else:
        resp = ask(client, content=b"x" * 20_000, headers={"content-type": "application/json"})
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])


# --- per-request call budget under concurrency ----------------------------------------------------
class CountingSlowFake:
    """Extractive like FakeLLM, but slow, so 8 requests overlap; tracks concurrency."""

    name = "fake"

    def __init__(self):
        self.lock, self.calls, self.active, self.max_active = threading.Lock(), 0, 0, 0

    def complete(self, prompt):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.3)
        with self.lock:
            self.active -= 1
        return FakeLLM().complete(prompt)


def test_eight_parallel_requests_one_llm_call_each(client, llm, caplog):
    fake = llm(CountingSlowFake())
    with caplog.at_level(logging.INFO, logger="app.ask"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda i: ask(client, headers={"X-Request-ID": f"par-{i}"}), range(8)))
    assert [r.status_code for r in responses] == [200] * 8
    assert {r.json()["request_id"] for r in responses} == {f"par-{i}" for i in range(8)}
    assert fake.calls == 8 and fake.max_active >= 2                    # they really overlapped
    lines = [r.getMessage() for r in caplog.records if r.name == "app.ask"]
    per_request = {re.search(r"request_id=(\S+)", line).group(1): re.search(r"llm_calls=(\d+)", line).group(1)
                   for line in lines}
    assert per_request == {f"par-{i}": "1" for i in range(8)}
