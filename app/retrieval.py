"""Retrieval: find the top-K chunks of the CALLER's company's ACTIVE policy, then prove it.

Flow of `retrieve()`:
  1. The company comes from the verified Principal only (never from the request).
  2. A Retriever (vector now; hybrid in P8, same interface) runs in a worker thread, bounded by
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
import time
from dataclasses import dataclass
from typing import Protocol

from chromadb.errors import NotFoundError

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
    similarity: float          # cosine similarity, 1 = identical direction


@dataclass(frozen=True)
class RetrievalResult:
    company_id: str
    version: int | None        # None: the company has no active policy
    chunks: list[RetrievedChunk]
    retrieve_ms: float

    @property
    def best_similarity(self) -> float | None:
        return self.chunks[0].similarity if self.chunks else None


class Retriever(Protocol):
    """Interface every retrieval mode implements. Runs in a worker thread (blocking is fine)."""

    name: str

    def search(self, store: Store, active: ActivePolicy, question: str, k: int) -> list[RetrievedChunk]: ...


def _to_chunk(hit: dict, similarity: float) -> RetrievedChunk:
    m = hit["metadata"]
    return RetrievedChunk(hit["id"], hit["text"], m["company_id"], m["doc_id"], m["version"],
                          m["section_id"], m["section_title"], similarity)


class VectorRetriever:
    name = "vector"

    def search(self, store: Store, active: ActivePolicy, question: str, k: int) -> list[RetrievedChunk]:
        hits = store.query(active.collection, question, k)
        return [_to_chunk(h, 1.0 - h["distance"]) for h in hits]    # cosine space: distance = 1 - cos


RETRIEVERS: dict[str, Retriever] = {"vector": VectorRetriever()}   # P8 adds "hybrid"


def get_retriever(mode: str) -> Retriever:
    if mode not in RETRIEVERS:
        raise NotImplementedError(f"retrieval mode {mode!r} is not built yet (hybrid lands in P8)")
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
            chunks = await asyncio.wait_for(
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
    assert_isolation(chunks, company_id, active.version)
    return RetrievalResult(company_id, active.version, chunks, (time.perf_counter() - started) * 1000)


def passes_prefilter(result: RetrievalResult, min_similarity: float) -> bool:
    """False -> answer not_covered without calling the LLM (reason low_relevance / no_active_policy)."""
    best = result.best_similarity
    return best is not None and best >= min_similarity
