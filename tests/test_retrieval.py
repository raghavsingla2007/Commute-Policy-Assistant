"""P4: retrieval over the caller's active collection, isolation assertion, timeout, pre-filter."""

import asyncio
import dataclasses
import time
from pathlib import Path

import pytest

from app import retrieval
from app.auth import Principal
from app.config import KNOWN_COMPANIES, load_settings
from app.embeddings import shared_embedder
from app.ingest import ingest_policy, seed_if_empty
from app.retrieval import (IsolationViolation, RetrievalError, RetrievalTimeout, RetrievedChunk,
                           get_retriever, passes_prefilter, retrieve)
from app.store import Store
from eval.freeze import load_cases

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"
ACTIVE_VERSION = {"kestrel": 2, "tamarind": 1, "orbitron": 1}
CANARIES = {"kestrel": ["4411", "Falcon Fleet"], "tamarind": ["5523", "Monsoon Cabs"],
            "orbitron": ["7720", "Nimbus Rides"]}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """The eval's state: v1 of all three companies seeded, then Kestrel upgraded to v2."""
    s = Store(tmp_path_factory.mktemp("retrieval") / "data", shared_embedder())
    seed_if_empty(s, POLICIES)
    ingest_policy(s, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    yield s
    s.close()


@pytest.fixture
def settings():
    return load_settings()


def who(company: str) -> Principal:
    return Principal(company, "employee", "test-user")


def run(coro):
    return asyncio.run(coro)


# --- basic behaviour ---------------------------------------------------------------------
def test_returns_top_k_from_active_version_with_similarity(store, settings):
    result = run(retrieve(store, who("kestrel"), "After what time do women get a security escort?", settings))
    assert (result.company_id, result.version) == ("kestrel", 2)
    assert len(result.chunks) == settings.retrieval_k == 3
    assert result.chunks[0].section_id == "4" and "9:00 PM" in result.chunks[0].text
    sims = [c.similarity for c in result.chunks]
    assert sims == sorted(sims, reverse=True) and all(-1.0 <= s <= 1.0 for s in sims)
    assert result.best_similarity == sims[0] and result.retrieve_ms > 0


@pytest.mark.parametrize("k", [1, 2, 3])
def test_k_comes_from_config(store, settings, k):
    result = run(retrieve(store, who("orbitron"), "weekend pickups", dataclasses.replace(settings, retrieval_k=k)))
    assert len(result.chunks) == k


def test_company_without_active_policy(tmp_path, settings):
    s = Store(tmp_path / "data", shared_embedder())
    try:
        result = run(retrieve(s, who("tamarind"), "escort?", settings))
        assert (result.version, result.chunks, result.best_similarity) == (None, [], None)
        assert not passes_prefilter(result, 0.0)
    finally:
        s.close()


def test_hybrid_mode_is_not_built_yet():
    assert get_retriever("vector").name == "vector"
    with pytest.raises(NotImplementedError, match="P8"):
        get_retriever("hybrid")


# --- isolation: every company asks every other company's questions and canary bait ----------
def bait_questions() -> list[str]:
    questions = [c["input"]["question"] for c in load_cases()]
    for company, (ext, vendor) in CANARIES.items():
        questions += [f"What is {vendor}' helpline ext. {ext}?", f"Book me a {vendor} cab",
                      f"Call extension {ext}", f"What is {company}'s escort rule?"]
    return questions


def test_no_foreign_chunk_for_any_company_and_any_question(store, settings):
    questions = bait_questions()
    checked = 0
    for company in sorted(KNOWN_COMPANIES):
        foreign = [c for other, cs in CANARIES.items() if other != company for c in cs]
        for q in questions:
            result = run(retrieve(store, who(company), q, settings))
            assert result.version == ACTIVE_VERSION[company]
            for chunk in result.chunks:
                assert (chunk.company_id, chunk.version) == (company, ACTIVE_VERSION[company]), q
                assert not any(f in chunk.text for f in foreign), (company, q, chunk.chunk_id)
                checked += 1
    assert checked == 3 * len(questions) * 3          # 3 companies x questions x k=3


# --- isolation assertion: forced bad chunks -------------------------------------------------
class ForcedRetriever:
    name = "vector"

    def __init__(self, chunks=None, sleep_s=0.0, error=None):
        self.chunks, self.sleep_s, self.error = chunks or [], sleep_s, error

    def search(self, store, active, question, k):
        time.sleep(self.sleep_s)
        if self.error:
            raise self.error
        return self.chunks


def fake_chunk(company: str, version: int) -> RetrievedChunk:
    return RetrievedChunk(f"{company}:v{version}:s4:p1", "Section 4 ...", company, f"{company}-doc", version,
                          "4", "Night Shift Safety", 0.9)


@pytest.mark.parametrize("bad", [fake_chunk("orbitron", 1), fake_chunk("kestrel", 1)],
                         ids=["foreign_company", "retired_version"])
def test_isolation_assertion_fires_on_forced_bad_chunk(store, settings, monkeypatch, bad):
    good = fake_chunk("kestrel", 2)
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", ForcedRetriever([good, bad]))
    with pytest.raises(IsolationViolation):
        run(retrieve(store, who("kestrel"), "escort?", settings))


def test_isolation_violation_is_a_retrieval_error():
    assert issubclass(IsolationViolation, RetrievalError) and issubclass(RetrievalTimeout, RetrievalError)


# --- timeout and failures -------------------------------------------------------------------
def test_slow_search_times_out_at_configured_limit(store, settings, monkeypatch):
    assert settings.retrieval_timeout_s == 0.5
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", ForcedRetriever([fake_chunk("kestrel", 2)], sleep_s=1.5))

    async def timed():
        # Timed inside the event loop: asyncio.run() itself waits for worker threads at shutdown,
        # which a long-running server loop never does.
        started = time.perf_counter()
        with pytest.raises(RetrievalTimeout):
            await retrieve(store, who("kestrel"), "escort?", settings)
        return time.perf_counter() - started

    elapsed = run(timed())
    assert 0.45 <= elapsed < 1.0, elapsed     # returned at ~0.5 s, did not wait for the 1.5 s search


def test_search_failure_becomes_retrieval_error(store, settings, monkeypatch):
    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", ForcedRetriever(error=RuntimeError("collection gone")))
    with pytest.raises(RetrievalError, match="collection gone"):
        run(retrieve(store, who("kestrel"), "escort?", settings))


# --- pre-filter -------------------------------------------------------------------------------
def test_prefilter(store, settings):
    result = run(retrieve(store, who("kestrel"), "security escort night drops", settings))
    best = result.best_similarity
    assert passes_prefilter(result, best) and not passes_prefilter(result, best + 0.001)


def test_default_threshold_is_low(settings):
    assert 0.0 < settings.not_covered_min_sim <= 0.3   # pre-filter only; the LLM verdict is the gate (D9)


# --- P6: upgrade edge — the old collection is deleted between reading the pointer and searching ------
def test_collection_dropped_mid_request_retries_once_on_new_version(tmp_path, settings, caplog):
    s = Store(tmp_path / "data", shared_embedder())
    try:
        ingest_policy(s, "kestrel", (POLICIES / "kestrel_v1.md").read_text(encoding="utf-8"))
        stale = s.active_policy("kestrel")                       # what a reader saw just before the flip
        ingest_policy(s, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))  # v1 dropped
        real, calls = s.active_policy, []

        def first_stale(company_id):
            calls.append(company_id)
            return stale if len(calls) == 1 else real(company_id)

        s.active_policy = first_stale
        with caplog.at_level("INFO", logger="app.retrieval"):
            result = run(retrieve(s, who("kestrel"), "security escort night drops", settings))
        assert result.version == 2 and {c.version for c in result.chunks} == {2}
        assert len(calls) == 2                                   # registry re-read exactly once
        assert "pol_kestrel_v1 gone (upgraded to v2); retrying once" in caplog.text
    finally:
        s.close()


def test_collection_missing_without_upgrade_is_an_error(tmp_path, settings):
    s = Store(tmp_path / "data", shared_embedder())
    try:
        ingest_policy(s, "kestrel", (POLICIES / "kestrel_v1.md").read_text(encoding="utf-8"))
        s.drop_collection("pol_kestrel_v1")                      # broken state: pointer says v1, data gone
        with pytest.raises(RetrievalError, match="not found"):
            run(retrieve(s, who("kestrel"), "escort", settings))
    finally:
        s.close()
