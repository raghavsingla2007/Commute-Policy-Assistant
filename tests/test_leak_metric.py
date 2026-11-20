"""P7 step A: the eval's leak metric is not vacuous.

A test-only "leaky" retriever (registered only inside this test; production config can't select it)
returns another company's chunks. With every safety layer bypassed, the eval's own leak check must
report the leak (foreign chunk retrieved and cited, foreign canary in the answer). With the isolation
assertion ON, the same setup gives a 503 with no foreign text. So "leaks = 0" in a real run means the
metric looked and found nothing, not that it can't see anything.
"""

import pytest
from fastapi.testclient import TestClient

from app import ask as ask_mod
from app import retrieval
from app.config import load_settings
from app.main import app
from app.retrieval import Hits, _to_chunk, get_retriever
from eval.run_eval import ask_http, check_case, find_leaks, section_texts, summarise

PROBE = {"id": "leak-probe", "input": {"company": "kestrel", "question": "Who operates the transport, and what is the transport desk helpline for things not covered here?"},
         "expected": {"status": ["not_covered"], "sections": [], "must_include": [], "must_not_include": []},
         "actual": None, "pass": False, "notes": "category=leak_bait; split=dev; test only"}


class LeakyRetriever:
    """TEST ONLY: ignores the caller's collection and searches Orbitron's."""

    name = "vector"

    def search(self, store, active, question, k):
        return Hits.of([_to_chunk(h, 1.0 - h["distance"]) for h in store.query("pol_orbitron_v1", question, k)])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as c:
        outcomes = {}
        app.state.result_sink = outcomes.__setitem__
        c.outcomes = outcomes
        yield c
        app.state.result_sink = None


def test_leaky_mode_is_not_selectable_in_production(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "leaky")
    with pytest.raises(ValueError, match="RETRIEVAL_MODE"):
        load_settings().validate()
    with pytest.raises(NotImplementedError):
        get_retriever("leaky")


def test_find_leaks_unit():
    clean = {"answer": "Escort after 9 PM.", "retrieved_companies": ["kestrel"] * 3,
             "cited_doc_ids": ["kestrel-transport-policy"]}
    assert find_leaks("kestrel", clean) == []
    dirty = {"answer": "Call Nimbus Rides on ext. 7720.", "retrieved_companies": ["kestrel", "orbitron"],
             "cited_doc_ids": ["orbitron-transport-guidelines"]}
    assert find_leaks("kestrel", dirty) == ["canary:7720", "canary:Nimbus Rides", "retrieved:orbitron",
                                            "cited:orbitron-transport-guidelines"]


def test_metric_sees_leaks_when_all_defences_are_bypassed(client, monkeypatch):
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", LeakyRetriever())
    monkeypatch.setattr(retrieval, "assert_isolation", lambda *a, **k: None)      # defence 1 off
    monkeypatch.setattr(ask_mod, "foreign_companies", lambda *a, **k: [])         # defence 2 off
    actual = ask_http(client, app, PROBE, client.outcomes)
    assert actual["http_status"] == 200 and actual["status"] == "answered"
    scores, ok = check_case(PROBE, actual, section_texts())
    actual["scores"] = scores
    assert not ok
    assert "retrieved:orbitron" in scores["leaks"] and "cited:orbitron-transport-guidelines" in scores["leaks"]
    assert any(leak.startswith("canary:") for leak in scores["leaks"])
    record = {**PROBE, "actual": actual, "pass": ok}
    assert summarise([record])["leaks"] == 1


def test_isolation_assertion_on_gives_503_and_no_foreign_text(client, monkeypatch):
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", LeakyRetriever())
    actual = ask_http(client, app, PROBE, client.outcomes)
    assert actual["http_status"] == 503 and actual["status"] == "unavailable"
    assert actual["reason"] == "IsolationViolation"
    body = (actual.get("answer") or "") + (actual.get("message") or "")
    for foreign in ("Nimbus", "7720", "Orbitron", "orbitron"):
        assert foreign not in body
    assert find_leaks("kestrel", actual) == []        # nothing foreign reached the user or the citations



# --- hybrid mode: the realistic hybrid failure is a wrong BM25 index under the caller's cache key ---------
@pytest.fixture
def hybrid_client(tmp_path, monkeypatch):
    from app.retrieval import HybridRetriever

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    h = HybridRetriever()
    monkeypatch.setitem(retrieval.RETRIEVERS, "hybrid", h)
    with TestClient(app) as c:
        outcomes = {}
        app.state.result_sink = outcomes.__setitem__
        c.outcomes = outcomes
        store = app.state.store
        key = (str(store.db_path), "pol_kestrel_v1")
        h._cache[key] = h.index(store, store.active_policy("orbitron"))        # Orbitron's index, Kestrel's key
        yield c
        app.state.result_sink = None


def test_hybrid_foreign_index_with_defences_bypassed_is_reported(hybrid_client, monkeypatch):
    monkeypatch.setattr(retrieval, "assert_isolation", lambda *a, **k: None)
    monkeypatch.setattr(ask_mod, "foreign_companies", lambda *a, **k: [])
    actual = ask_http(hybrid_client, app, PROBE, hybrid_client.outcomes)
    scores, _ = check_case(PROBE, actual, section_texts())
    assert "retrieved:orbitron" in scores["leaks"]


def test_hybrid_foreign_index_with_isolation_on_gives_503(hybrid_client):
    actual = ask_http(hybrid_client, app, PROBE, hybrid_client.outcomes)
    assert actual["http_status"] == 503 and actual["reason"] == "IsolationViolation"
    assert find_leaks("kestrel", actual) == []
