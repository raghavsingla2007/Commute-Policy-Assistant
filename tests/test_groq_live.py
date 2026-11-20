"""Real Groq calls. Skipped by default; run with:
    docker compose run --rm -e RUN_GROQ_TESTS=1 api pytest -m groq
"""

import asyncio
from pathlib import Path

import pytest

from app.ask import AskContext, answer
from app.auth import Principal
from app.config import load_settings
from app.embeddings import shared_embedder
from app.ingest import ingest_policy, seed_if_empty
from app.llm import GroqLLM
from app.store import Store
from tests.conftest import REAL_GROQ_KEY

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"


@pytest.mark.groq
def test_real_groq_answers_escort_question(tmp_path):
    s = load_settings()
    store = Store(tmp_path / "data", shared_embedder())
    llm = GroqLLM(REAL_GROQ_KEY, s.llm_model, s.llm_timeout_s, s.llm_max_tokens, s.llm_reasoning_effort)
    try:
        seed_if_empty(store, POLICIES)
        ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
        res = asyncio.run(answer(Principal("kestrel", "employee", "live"),
                                 "After what time do women get a security escort on night drops?",
                                 AskContext(store, llm, s)))
        assert res.status == "answered" and res.llm_calls == 1
        assert "9" in res.answer and res.citations[0].section_id == "4"
    finally:
        llm.close()
        store.close()
