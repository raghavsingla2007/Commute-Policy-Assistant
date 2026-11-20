"""Retrieval: find the top-K chunks of the CALLER's company's ACTIVE policy, then prove it.

Flow of `retrieve()`:
  1. The company comes from the verified Principal only (never from the request).
  2. A Retriever (vector, or hybrid BM25 + vector; same interface) runs in a worker thread, bounded by
     RETRIEVAL_TIMEOUT_S with asyncio.wait_for. On timeout we raise RetrievalTimeout (the API maps it
     to "try again"). The worker thread can't be killed; it finishes in the background and its result
     is discarded.
  3. Isolation assertion: every chunk must carry the caller's company_id and the active version.
     Collections are already per company, so this only fires if *our* code has a bug; then we refuse
     to answer (IsolationViolation -> "unavailable") rather than risk showing another company's rule.

`passes_prefilter()` is the cheap not-covered pre-filter: if even the best chunk is below
NOT_COVERED_MIN_SIM, skip the LLM. It is set LOW on purpose (chosen from DEV data, see PROJECT_LOG
§8.6): it only removes clearly unrelated questions. The LLM's verdict is the real gate (D9).
"""

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from chromadb.errors import NotFoundError
from rank_bm25 import BM25Okapi

from app.auth import Principal
from app.config import Settings
from app.store import ActivePolicy, Store


log = logging.getLogger("app.retrieval")


class RetrievalError(Exception):
    """Base class: anything that means "we can't answer safely right now" (API: 503 unavailable)."""


class RetrievalTimeout(RetrievalError):
    pass


class IsolationViolation(RetrievalError):
    pass


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    company_id: str
    doc_id: str
    version: int
    section_id: str
    section_title: str
    similarity: float | None   # vector cosine similarity (1 = same direction); None if only BM25 found it


@dataclass(frozen=True)
class Hits:
    """What a retriever returns: its top chunks, plus the TOP-1 VECTOR cosine similarity, which the
    not-covered pre-filter uses in every mode (BM25 scores are not calibrated, D84)."""

    chunks: list[RetrievedChunk]
    top_similarity: float | None

    @classmethod
    def of(cls, chunks: list[RetrievedChunk]) -> "Hits":
        return cls(chunks, chunks[0].similarity if chunks else None)


@dataclass(frozen=True)
class RetrievalResult:
    company_id: str
    version: int | None        # None: the company has no active policy
    chunks: list[RetrievedChunk]
    retrieve_ms: float
    best_similarity: float | None = None   # top-1 vector cosine similarity (pre-filter input)


class Retriever(Protocol):
    """Interface every retrieval mode implements. Runs in a worker thread (blocking is fine)."""

    name: str

    def search(self, store: Store, active: ActivePolicy, question: str, k: int) -> Hits: ...


def _to_chunk(hit: dict, similarity: float) -> RetrievedChunk:
    m = hit["metadata"]
    return RetrievedChunk(hit["id"], hit["text"], m["company_id"], m["doc_id"], m["version"],
                          m["section_id"], m["section_title"], similarity)


class VectorRetriever:
    name = "vector"

    def search(self, store: Store, active: ActivePolicy, question: str, k: int) -> Hits:
        hits = store.query(active.collection, question, k)
        return Hits.of([_to_chunk(h, 1.0 - h["distance"]) for h in hits])   # cosine space: distance = 1 - cos


# ---------------------------------------------------------------- hybrid: BM25 + vector, fused by RRF (P8)
#
# What it does: two rankers each pick their top 5 chunks from the caller's ACTIVE collection. The vector
# ranker matches meaning ("ride along" ~ "escort"); BM25 matches exact words ("cut-off", "Sunday", "₹").
# Reciprocal Rank Fusion scores each chunk as the sum of 1 / (60 + rank) over the lists it appears in,
# so a chunk ranked well by both wins, and the raw scores (cosine vs BM25, different scales) never mix.
# Why RRF and not reranking or query rewriting: RRF costs ~ms and 0 LLM calls. A cross-encoder reranker
# is another model and +100-300 ms; query rewriting needs the second LLM call (~1 s of the 3 s budget).
# Config is pre-registered (PROJECT_LOG §8.8): BM25Okapi defaults, N = 5, k = 60, equal weights.
#
# Isolation: an index is built only from ONE collection's own chunks, and is cached under
# (store path, collection name), where the name carries company and version (pol_kestrel_v2). After an
# upgrade the active collection name changes, so the old index is never looked up again (and is evicted).
# Thread safety: one lock guards the cache and every build happens inside it, so two threads never build
# or replace an entry at the same time; a built index is read-only, so queries use it without a lock.

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())        # lowercase, split on non-alphanumerics, keep digits


@dataclass(frozen=True)
class _Index:
    ids: list[str]
    texts: list[str]
    metadatas: list[dict]
    bm25: BM25Okapi


class HybridRetriever:
    name = "hybrid"
    N, RRF_K = 5, 60

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], _Index] = {}
        self._lock = threading.Lock()

    def index(self, store: Store, active: ActivePolicy) -> _Index:
        key = (str(store.db_path), active.collection)
        with self._lock:
            if key not in self._cache:
                got = store.get_collection(active.collection).get(include=["documents", "metadatas"])
                stale = [k for k in self._cache if k[0] == key[0] and k[1].startswith(f"pol_{active.company_id}_v")]
                for k in stale:                                       # retired versions of this company
                    del self._cache[k]
                self._cache[key] = _Index(got["ids"], got["documents"], got["metadatas"],
                                          BM25Okapi([tokenize(d) for d in got["documents"]]))
            return self._cache[key]

    def warm(self, store: Store) -> int:
        """Build every active collection's index at startup, so no request pays for it."""
        active = [store.active_policy(r["company_id"]) for r in store.rows() if r["status"] == "active"]
        for a in active:
            self.index(store, a)
        return len(active)

    def search(self, store: Store, active: ActivePolicy, question: str, k: int) -> Hits:
        vector = [_to_chunk(h, 1.0 - h["distance"]) for h in store.query(active.collection, question, self.N)]
        idx = self.index(store, active)
        scores = idx.bm25.get_scores(tokenize(question))
        bm25_top = [i for i in sorted(range(len(scores)), key=lambda i: -scores[i])[: self.N] if scores[i] > 0]
        fused: dict[str, float] = {}
        for rank, c in enumerate(vector, start=1):
            fused[c.chunk_id] = fused.get(c.chunk_id, 0.0) + 1 / (self.RRF_K + rank)
        for rank, i in enumerate(bm25_top, start=1):
            fused[idx.ids[i]] = fused.get(idx.ids[i], 0.0) + 1 / (self.RRF_K + rank)
        by_id = {c.chunk_id: c for c in vector}
        for i in bm25_top:                                            # BM25-only finds: no cosine score
            by_id.setdefault(idx.ids[i], _to_chunk({"id": idx.ids[i], "text": idx.texts[i],
                                                    "metadata": idx.metadatas[i]}, None))
        order = sorted(fused, key=lambda cid: -fused[cid])            # stable: ties keep vector order
        return Hits([by_id[cid] for cid in order[:k]], vector[0].similarity if vector else None)


RETRIEVERS: dict[str, Retriever] = {"vector": VectorRetriever(), "hybrid": HybridRetriever()}


def get_retriever(mode: str) -> Retriever:
    if mode not in RETRIEVERS:
        raise NotImplementedError(f"unknown retrieval mode {mode!r} (known: {sorted(RETRIEVERS)})")
    return RETRIEVERS[mode]


def assert_isolation(chunks: list[RetrievedChunk], company_id: str, version: int) -> None:
    for c in chunks:
        if c.company_id != company_id or c.version != version:
            raise IsolationViolation(
                f"chunk {c.chunk_id} is {c.company_id} v{c.version}; caller is {company_id} v{version}")


async def retrieve(store: Store, principal: Principal, question: str, settings: Settings) -> RetrievalResult:
    """One retrieval budget (RETRIEVAL_TIMEOUT_S) covers at most two attempts.

    Upgrade edge (P6): between reading the active pointer and querying, a concurrent upgrade can flip
    to v2 and delete v1's collection. Chroma then raises NotFoundError. We re-read the registry ONCE
    and, if the active version changed, search the new collection with the time left. If it did not
    change, or the retry fails too, it is a RetrievalError ("try again"). This is not an LLM retry.
    """
    started = time.perf_counter()
    deadline = started + settings.retrieval_timeout_s
    company_id = principal.company_id                                  # the ONLY source of the company
    retriever = get_retriever(settings.retrieval_mode)
    active = store.active_policy(company_id)
    for attempt in (1, 2):
        if active is None:
            return RetrievalResult(company_id, None, [], (time.perf_counter() - started) * 1000)
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise RetrievalTimeout(f"retrieval exceeded {settings.retrieval_timeout_s}s")
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(retriever.search, store, active, question, settings.retrieval_k),
                timeout=remaining,
            )
            break
        except asyncio.TimeoutError:
            raise RetrievalTimeout(f"retrieval exceeded {settings.retrieval_timeout_s}s") from None
        except NotFoundError as exc:
            fresh = store.active_policy(company_id)
            if attempt == 2 or fresh is None or fresh.version == active.version:
                raise RetrievalError(f"collection {active.collection} not found") from exc
            log.info("retrieval: %s gone (upgraded to v%s); retrying once", active.collection, fresh.version)
            active = fresh
        except Exception as exc:     # anything else: "try again", never a half answer
            raise RetrievalError(f"{type(exc).__name__}: {exc}") from exc
    assert_isolation(hits.chunks, company_id, active.version)
    return RetrievalResult(company_id, active.version, hits.chunks, (time.perf_counter() - started) * 1000,
                           hits.top_similarity)


def passes_prefilter(result: RetrievalResult, min_similarity: float) -> bool:
    """False -> answer not_covered without calling the LLM (reason low_relevance / no_active_policy)."""
    best = result.best_similarity
    return best is not None and best >= min_similarity
