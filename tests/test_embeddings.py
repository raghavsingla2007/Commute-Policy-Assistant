"""The fast embedder must produce the same vectors as Chroma's stock embedder."""

import numpy as np
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from app.embeddings import MiniLMEmbedder


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
