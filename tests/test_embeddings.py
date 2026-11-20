"""The fast embedder must produce the same vectors as Chroma's stock embedder."""

import numpy as np
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from app.embeddings import MiniLMEmbedder, shared_embedder


def test_same_vectors_as_stock_embedder():
    texts = [
        "Can I book a cab on Saturday?",
        "Section 4 Night Shift Safety\nWomen employees are provided a security escort on drops "
        "departing after 9:00 PM. Drops departing at or before 9:00 PM do not include a security escort.",
    ]
    stock = np.array(DefaultEmbeddingFunction()(texts))
    fast = np.array(MiniLMEmbedder()(texts))
    assert fast.shape == stock.shape == (2, 384)
    assert np.abs(fast - stock).max() < 1e-5


# --- D31: ingest and search go through the SAME embedder (same object, same vectors) ----------
class RecordingEmbedder:
    """Wraps the real embedder and records every text it embeds."""

    def __init__(self, real):
        self.real, self.texts = real, []

    def __call__(self, texts):
        self.texts += list(texts)
        return self.real(texts)


def test_ingest_and_search_share_one_embedder(tmp_path):
    from pathlib import Path

    from app.ingest import build_chunks, ingest_policy, parse_policy
    from app.store import Store

    policy = (Path(__file__).resolve().parent.parent / "data" / "policies" / "kestrel_v2.md").read_text()
    spy = RecordingEmbedder(shared_embedder())
    store = Store(tmp_path / "data", spy)
    ingest_policy(store, "kestrel", policy)
    chunks = build_chunks(parse_policy(policy))
    assert set(c.text for c in chunks) <= set(spy.texts)               # documents went through it

    spy.texts.clear()
    question = "After what time do women get an escort?"
    store.search_active("kestrel", question, k=3)
    assert spy.texts == [question]                                     # the query went through it too

    # Same vectors: what Chroma stored for a chunk == what the search-time path makes for that text,
    # so searching with a chunk's own text finds that chunk at distance ~0.
    col = store.get_collection("pol_kestrel_v2")
    target = chunks[3]
    stored = np.array(col.get(ids=[target.chunk_id], include=["embeddings"])["embeddings"][0])
    assert np.abs(stored - np.array(spy.real([target.text])[0])).max() < 1e-6
    [top] = store.search_active("kestrel", target.text, k=1)
    assert top["id"] == target.chunk_id and top["distance"] < 1e-5


def test_collections_have_no_embedding_function_of_their_own(tmp_path):
    """Chroma can't fall back to its own (256-padded) embedder: text-only queries must fail."""
    import pytest

    from app.ingest import ingest_policy
    from app.store import Store
    from pathlib import Path

    store = Store(tmp_path / "data", shared_embedder())
    policy = (Path(__file__).resolve().parent.parent / "data" / "policies" / "orbitron_v1.md").read_text()
    ingest_policy(store, "orbitron", policy)
    with pytest.raises(ValueError, match="must provide an embedding function"):
        store.get_collection("pol_orbitron_v1").query(query_texts=["escort"], n_results=1)
