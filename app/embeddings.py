"""The one embedding function used for BOTH policy chunks (at ingest) and questions (at search).

WHY THIS FILE EXISTS (plain English)
------------------------------------
To search, we turn text into a list of 384 numbers (a vector) with a small local model,
all-MiniLM-L6-v2. Chroma ships this exact model. We use Chroma's copy, so it is the same model and
the same weights.

The problem: Chroma's wrapper pads every input to 256 tokens before running the model. A 7-word
question becomes 256 tokens of mostly padding, and the model still processes all 256 positions.
In our Docker image that took ~490 ms per question, about a sixth of the 3-second budget, spent on
padding.

The fix: pad each text only to its own length. Padding positions are masked out by the model's
attention mask and ignored by the mean-pooling step, so the output vector is the same. The test
measures a max difference of ~1e-7 (tests/test_embeddings.py). A question now takes ~26 ms.

Why one text per call: Chroma tokenizes texts one by one and stacks them. Texts of different
lengths cannot be stacked, so we call the model once per text. That is fine at our sizes.

Why ingest and search must share it: a question vector is only comparable to chunk vectors made
by the same function. Store owns ONE embedder object, ingest.py and Store.search_active both call
`store.embedder`, and Chroma collections are created with embedding_function=None, so Chroma can
never silently embed with a different function. tests/test_embeddings.py proves all of this.
"""

import functools

from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2


class MiniLMEmbedder:
    def __init__(self) -> None:
        self._model = ONNXMiniLM_L6_V2()
        # No `length=` -> pad to the longest input in the call. We call with one text at a time,
        # because Chroma tokenizes texts separately and cannot stack different lengths.
        self._model.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def __call__(self, texts: list[str]) -> list:
        return [self._model([text])[0] for text in texts]


@functools.cache
def shared_embedder() -> MiniLMEmbedder:
    """One model per process. The app (and every app restart inside the test suite) reuses it."""
    return MiniLMEmbedder()
