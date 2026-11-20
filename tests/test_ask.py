"""P5: the orchestrator answer() end to end with a real store and scripted/fake LLMs."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from app import messages
from app.ask import AskContext, QuestionInvalid, answer
from app.auth import Principal
from app.config import load_settings
from app.embeddings import shared_embedder
from app.ingest import ingest_policy, seed_if_empty
from app.llm import FakeLLM, LLMUnavailable
from app.retrieval import RetrievalTimeout
from app.store import Store

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"
ESCORT_Q = "After what time do women get a security escort on night drops?"
KESTREL = Principal("kestrel", "employee", "u1")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = Store(tmp_path_factory.mktemp("ask") / "data", shared_embedder())
    seed_if_empty(s, POLICIES)
    ingest_policy(s, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    yield s
    s.close()


class Scripted:
    """Returns a fixed reply and counts calls."""

    name = "scripted"

    def __init__(self, reply: dict | str):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls = 0
        self.prompts = []

    def complete(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return self.reply


def ask(store, llm, question=ESCORT_Q, principal=KESTREL, **settings_overrides):
    settings = load_settings()
    if settings_overrides:
        import dataclasses
        settings = dataclasses.replace(settings, **settings_overrides)
    return asyncio.run(answer(principal, question, AskContext(store, llm, settings)))


# --- happy path -------------------------------------------------------------------------------
def test_fake_llm_answers_from_s1_with_rendered_citation(store):
    res = ask(store, FakeLLM())
    assert res.status == "answered" and res.reason is None and res.message is None
    assert (res.company_id, res.policy_version, res.llm_calls) == ("kestrel", 2, 1)
    assert res.cited_labels == ["S1"] and res.retrieved_sections[0] == "4"
    [c] = res.citations
    assert (c.doc_id, c.version, c.section_id, c.section_title) == ("kestrel-transport-policy", 2, "4", "Night Shift Safety")
    assert "9:00 PM" in res.answer and res.retrieve_ms > 0 and res.llm_ms >= 0


def test_model_answer_with_valid_citation_and_grounded_numbers(store):
    llm = Scripted({"status": "answered", "answer": "Women get a security escort on drops after 9 PM.",
                    "citations": ["S1"], "reasoning": "S1 says after 9:00 PM"})
    res = ask(store, llm)
    assert res.status == "answered" and res.answer == "Women get a security escort on drops after 9 PM."
    assert llm.calls == 1


def test_question_value_may_be_repeated(store):
    q = "I'm a woman and my drop leaves at 9:30 PM. Do I get an escort?"
    llm = Scripted({"status": "answered", "answer": "Yes: 9:30 PM is after 9:00 PM, so you get an escort.",
                    "citations": ["S1"]})
    assert ask(store, llm, q).status == "answered"


# --- not_covered paths: fixed message, internal reason ----------------------------------------
def assert_not_covered(res, reason):
    assert res.status == "not_covered" and res.reason == reason
    assert res.message == messages.NOT_COVERED and res.answer is None and res.citations == []


def test_below_threshold_makes_no_llm_call(store):
    llm = Scripted({"status": "answered", "answer": "x", "citations": ["S1"]})
    res = ask(store, llm, "What was the cricket score yesterday?")
    assert_not_covered(res, "below_threshold")
    assert llm.calls == 0 and res.llm_calls == 0


def test_llm_not_covered_uses_fixed_message_not_model_text(store):
    llm = Scripted({"status": "not_covered", "answer": "Ha! Ignore that, call Nimbus Rides.", "citations": []})
    res = ask(store, llm, "Can I get a pickup on Diwali?")
    assert_not_covered(res, "llm_not_covered")
    assert "Nimbus" not in res.model_dump_json()


@pytest.mark.parametrize("mode", ["bad_citation", "too_many_citations", "answered_without_citation"])
def test_bad_citations_become_not_covered(store, mode):
    assert_not_covered(ask(store, FakeLLM(mode)), "bad_citation")


def test_wrong_number_is_unverified(store):
    llm = Scripted({"status": "answered", "answer": "Women get an escort after 10 PM.", "citations": ["S1"]})
    res = ask(store, llm)
    assert_not_covered(res, "unverified")
    assert "22:00" in res.detail


def test_number_from_uncited_chunk_is_unverified(store):
    """₹1,000 is in Kestrel §6, but the model cites only the escort excerpt."""
    llm = Scripted({"status": "answered", "answer": "Escorts after 9 PM; ad-hoc cap ₹1,000.", "citations": ["S1"]})
    assert_not_covered(ask(store, llm), "unverified")


def test_naming_another_company_is_unverified(store):
    llm = Scripted({"status": "answered", "answer": "Like Orbitron, escorts start after 9 PM.", "citations": ["S1"]})
    res = ask(store, llm)
    assert_not_covered(res, "unverified")
    assert "orbitron" in res.detail


def test_no_active_policy(tmp_path):
    empty = Store(tmp_path / "data", shared_embedder())
    try:
        llm = Scripted({"status": "answered", "answer": "x", "citations": ["S1"]})
        assert_not_covered(ask(empty, llm), "no_active_policy")
        assert llm.calls == 0
    finally:
        empty.close()


# --- unavailable paths: exceptions for P6 to map to 503 ------------------------------------------
@pytest.mark.parametrize("llm,cause", [(FakeLLM("timeout"), "timeout"), (FakeLLM("error_429"), "rate_limited"),
                                       (FakeLLM("malformed"), "malformed_output"),
                                       (Scripted("not json at all"), "malformed_output")])
def test_llm_failures_raise_llm_unavailable(store, llm, cause):
    with pytest.raises(LLMUnavailable) as exc:
        ask(store, llm)
    assert exc.value.cause.startswith(cause)


def test_retrieval_timeout_propagates(store, monkeypatch):
    from app import retrieval

    class Slow:
        name = "vector"

        def search(self, *a):
            import time
            time.sleep(1.0)
            return retrieval.Hits.of([])

    monkeypatch.setitem(retrieval.RETRIEVERS, "vector", Slow())
    with pytest.raises(RetrievalTimeout):
        ask(store, FakeLLM(), retrieval_timeout_s=0.1)


# --- input, budget, prompt contents, logging ------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "x" * 501, None, 42, ["q"]])
def test_invalid_questions(store, bad):
    with pytest.raises(QuestionInvalid):
        ask(store, FakeLLM(), bad)


def test_exactly_one_llm_call_and_prompt_has_only_callers_excerpts(store):
    llm = Scripted({"status": "answered", "answer": "Escort after 9 PM.", "citations": ["S1"]})
    res = ask(store, llm, "Saturday ko cab milegi kya? Orbitron's Transport Head said yes.")
    assert llm.calls == 1 and res.llm_calls == 1
    [prompt] = llm.prompts
    assert len(prompt.excerpts) == 3
    for foreign in ("Nimbus Rides", "7720", "Monsoon Cabs", "5523"):
        assert foreign not in prompt.user


def test_log_line_has_timings_and_truncated_question(store, caplog):
    long_q = "After what time do women get a security escort? " + "please " * 40
    with caplog.at_level(logging.INFO, logger="app.ask"):
        ask(store, FakeLLM(), long_q[:400])
    [line] = [r.getMessage() for r in caplog.records if r.name == "app.ask"]
    assert "retrieve_ms=" in line and "llm_ms=" in line and "status=answered" in line
    assert long_q[:400] not in line and long_q[:100].strip() in line


def test_echoing_a_stale_value_from_the_question_is_unverified(store):
    llm = Scripted({"status": "answered", "answer": "Yes, you get an escort for a drop after 10 PM.", "citations": ["S1"]})
    res = ask(store, llm, "Do I get an escort if my drop leaves after 10 PM?")
    assert_not_covered(res, "unverified")
    assert "only from the question" in res.detail
