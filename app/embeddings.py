"""Local embedding model: Chroma's ONNX all-MiniLM-L6-v2, padded per text instead of to 256 tokens.

Chroma's stock tokenizer pads every input to 256 tokens, so a 7-word question costs a full
256-token forward pass (~490 ms in our Docker image). Padding each text only to its own length
gives the same vectors (max diff ~1e-7, see tests/test_embeddings.py) at ~26 ms per query.
"""

from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2


class MiniLMEmbedder:
    def __init__(self) -> None:
        self._model = ONNXMiniLM_L6_V2()
        # No `length=` -> pad to the longest input in the call. We call with one text at a time,
        # because Chroma tokenizes texts separately and cannot stack different lengths.
        self._model.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def __call__(self, texts: list[str]) -> list:
        return [self._model([text])[0] for text in texts]
