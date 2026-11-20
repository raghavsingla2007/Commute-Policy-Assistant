"""P8: hybrid retrieval (BM25 + vector, RRF). Isolation, cache invalidation after an upgrade, stale-cache
safety, warm-up, and the pre-registered configuration."""

import asyncio
import dataclasses
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rank_bm25 import BM25Okapi

from app import retrieval
from app.auth import Principal, mint_token
from app.config import load_settings
from app.embeddings import shared_embedder
from app.ingest import build_chunks, ingest_policy, parse_policy, seed_if_empty
from app.main import app
from app.retrieval import HybridRetriever, IsolationViolation, _Index, retrieve, tokenize
from app.store import Store

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"
KESTREL = Principal("kestrel", "employee", "t")


def hybrid_settings(**kw):
    return dataclasses.replace(load_settings(), retrieval_mode="hybrid", **kw)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data", shared_embedder())
    seed_if_empty(s, POLICIES)
    yield s
    s.close()


@pytest.fixture
def fresh_hybrid(monkeypatch):
    """A fresh HybridRetriever (empty cache) for this test only."""
    h = HybridRetriever()
    monkeypatch.setitem(retrieval.RETRIEVERS, "hybrid", h)
    return h


# --- the pre-registered configuration --------------------------------------------------------
def test_preregistered_config():
    assert (HybridRetriever.N, HybridRetriever.RRF_K) == (5, 60)
    bm = BM25Okapi([["a"], ["b"]])
    assert (bm.k1, bm.b, bm.epsilon) == (1.5, 0.75, 0.25)                    # library defaults, untouched
    assert load_settings().not_covered_min_sim == 0.15


def test_tokenizer():
    assert tokenize("Book a cab on Sunday, 6:00 PM — ₹1,200 per trip; ext. 7720!") == \
        ["book", "a", "cab", "on", "sunday", "6", "00", "pm", "1", "200", "per", "trip", "ext", "7720"]


def test_rrf_by_hand(store, fresh_hybrid):
    """The fused order equals the RRF formula applied to the two candidate lists."""
    from app.retrieval import VectorRetriever

    active = store.active_policy("tamarind")
    q = "By when must I request a cab for tomorrow?"
    vec = [c.chunk_id for c in VectorRetriever().search(store, active, q, 5).chunks]
    idx = fresh_hybrid.index(store, active)
    scores = idx.bm25.get_scores(tokenize(q))
    bm = [idx.ids[i] for i in sorted(range(len(scores)), key=lambda i: -scores[i])[:5] if scores[i] > 0]
    expected = {}
    for lst in (vec, bm):
        for rank, cid in enumerate(lst, start=1):
            expected[cid] = expected.get(cid, 0) + 1 / (60 + rank)
    want = sorted(expected, key=lambda cid: -expected[cid])[:3]
    got = fresh_hybrid.search(store, active, q, 3)
    assert [c.chunk_id for c in got.chunks] == want


def test_prefilter_input_is_vector_top1_in_hybrid_mode(store, fresh_hybrid):
    for q in ["What was the cricket score yesterday?", "After what time do women get an escort?"]:
        v = run(retrieve(store, KESTREL, q, load_settings()))
        h = run(retrieve(store, KESTREL, q, hybrid_settings()))
        assert h.best_similarity == v.best_similarity                      # same pre-filter decision
    assert h.best_similarity >= 0.15 and v.best_similarity >= 0.15


# --- isolation and cache invalidation ---------------------------------------------------------
def test_index_contains_only_its_own_collection(store, fresh_hybrid):
    for company in ("kestrel", "tamarind", "orbitron"):
        idx = fresh_hybrid.index(store, store.active_policy(company))
        assert {(m["company_id"], m["version"]) for m in idx.metadatas} == {(company, 1)}
        assert len(idx.ids) == store.get_collection(f"pol_{company}_v1").count()


def test_after_upgrade_hybrid_uses_only_v2(store, fresh_hybrid):
    """(1) Query v1 (index cached), upgrade, query again: only v2 chunks, no v1-only text, v1 index evicted."""
    q = "After what time do women get a security escort on night drops?"
    before = run(retrieve(store, KESTREL, q, hybrid_settings()))
    assert {c.version for c in before.chunks} == {1}
    path = str(store.db_path)
    assert (path, "pol_kestrel_v1") in fresh_hybrid._cache
    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    after = run(retrieve(store, KESTREL, q, hybrid_settings()))
    assert after.version == 2 and {c.version for c in after.chunks} == {2}
    v1_only = {c.text for c in build_chunks(parse_policy((POLICIES / "kestrel_v1.md").read_text(encoding="utf-8")))} - \
              {c.text for c in build_chunks(parse_policy((POLICIES / "kestrel_v2.md").read_text(encoding="utf-8")))}
    assert not {c.text for c in after.chunks} & v1_only
    assert all("10:00 PM" not in c.text for c in after.chunks)
    assert (path, "pol_kestrel_v1") not in fresh_hybrid._cache and (path, "pol_kestrel_v2") in fresh_hybrid._cache


def v1_index() -> _Index:
    chunks = build_chunks(parse_policy((POLICIES / "kestrel_v1.md").read_text(encoding="utf-8")))
    return _Index([c.chunk_id for c in chunks], [c.text for c in chunks], [c.metadata for c in chunks],
                  BM25Okapi([tokenize(c.text) for c in chunks]))


def test_stale_index_under_old_key_is_never_looked_up(store, fresh_hybrid):
    """(2a) A v1 index left in the cache under v1's key is ignored: the key comes from the ACTIVE collection."""
    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    fresh_hybrid._cache[(str(store.db_path), "pol_kestrel_v1")] = v1_index()
    res = run(retrieve(store, KESTREL, "escort after what time at night?", hybrid_settings()))
    assert {c.version for c in res.chunks} == {2}


def test_stale_index_under_new_key_is_caught_by_isolation(store, fresh_hybrid):
    """(2b) Even if a v1 index somehow sat under v2's key, its chunks carry version 1 and the isolation
    assertion refuses them (503 at the API)."""
    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    fresh_hybrid._cache[(str(store.db_path), "pol_kestrel_v2")] = v1_index()
    with pytest.raises(IsolationViolation, match="v1"):
        run(retrieve(store, KESTREL, "no-show suspension written warning calendar month", hybrid_settings()))


def test_no_foreign_chunk_in_hybrid_for_any_company_and_question(store, fresh_hybrid):
    """Same sweep as the vector test: all eval questions + canary bait, every company."""
    from tests.test_retrieval import ACTIVE_VERSION, CANARIES, bait_questions

    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    questions, checked = bait_questions(), 0
    for company in ("kestrel", "tamarind", "orbitron"):
        foreign = [c for other, cs in CANARIES.items() if other != company for c in cs]
        for q in questions:
            res = run(retrieve(store, Principal(company, "employee", "t"), q, hybrid_settings()))
            for c in res.chunks:
                assert (c.company_id, c.version) == (company, ACTIVE_VERSION[company]), q
                assert not any(f in c.text for f in foreign), (company, q)
                checked += 1
    assert checked == 3 * len(questions) * 3


# --- warm-up ----------------------------------------------------------------------------------
def test_startup_builds_indexes_so_first_request_does_not(tmp_path, monkeypatch, fresh_hybrid, caplog):
    """(4) A (re)start in hybrid mode builds all active indexes during warm-up."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    for start in (1, 2):                                                  # first start, then a restart
        fresh_hybrid._cache.clear()
        with caplog.at_level(logging.INFO, logger="app"):
            with TestClient(app) as client:
                path = str(app.state.store.db_path)
                keys = {k for k in fresh_hybrid._cache if k[0] == path}
                assert keys == {(path, "pol_kestrel_v1"), (path, "pol_orbitron_v1"), (path, "pol_tamarind_v1")}
                built = []
                original = fresh_hybrid.index

                def spy(store, active):
                    built.append(active.collection not in {k[1] for k in fresh_hybrid._cache})
                    return original(store, active)

                fresh_hybrid.index = spy
                tok = mint_token(app.state.settings, "kestrel", "employee", "t")
                r = client.post("/ask", headers={"Authorization": f"Bearer {tok}"},
                                json={"question": "After what time do women get an escort?"})
                fresh_hybrid.index = original
                assert r.status_code == 200 and built == [False]          # the first request found it built
        assert "bm25 indexes built: 3" in caplog.text
