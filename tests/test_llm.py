"""P5: GroqLLM over a mocked transport (no network), FakeLLM modes, call budget, strict output parsing."""

import json

import httpx
import pytest

from app.llm import (FAKE_MODES, CallBudget, CallBudgetExceeded, FakeLLM, GroqLLM, LLMUnavailable, Prompt,
                     parse_llm_output, strip_code_fences)

KEY = "gsk_test_" + "k" * 30          # fake key; must never appear in errors or repr
PROMPT = Prompt(system="SYS", user="USER", excerpts=[("S1", "Section 4 Night\nAfter 9:00 PM.")], question="q")
GOOD = {"status": "answered", "answer": "After 9:00 PM.", "citations": ["S1"]}


def groq(handler, **kw) -> GroqLLM:
    return GroqLLM(KEY, kw.get("model", "openai/gpt-oss-20b"), 2.2, 300, "low", transport=httpx.MockTransport(handler))


def ok_body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def failure(llm: GroqLLM) -> LLMUnavailable:
    with pytest.raises(LLMUnavailable) as exc:
        llm.complete(PROMPT)
    assert KEY not in str(exc.value) and KEY not in repr(exc.value)
    return exc.value


# --- Groq: success and request shape ----------------------------------------------------
def test_success_and_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body(json.dumps(GOOD)))

    llm = groq(handler)
    assert json.loads(llm.complete(PROMPT)) == GOOD
    body = seen["body"]
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert body["model"] == "openai/gpt-oss-20b" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"} and body["max_completion_tokens"] == 300
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
    assert body["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    assert KEY not in repr(llm)


def test_non_gpt_oss_model_gets_no_reasoning_params():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body(json.dumps(GOOD)))

    groq(handler, model="qwen/qwen3.8-27b").complete(PROMPT)
    assert "reasoning_effort" not in seen["body"] and "include_reasoning" not in seen["body"]


def test_json_wrapped_in_code_fences_is_unwrapped():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    assert json.loads(groq(lambda r: httpx.Response(200, json=ok_body(fenced))).complete(PROMPT)) == GOOD
    assert strip_code_fences("```\n{}\n```") == "{}" and strip_code_fences("  {} ") == "{}"


# --- Groq: every failure becomes LLMUnavailable with a cause -------------------------------
def test_429_with_retry_after():
    err = failure(groq(lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={"error": {}})))
    assert (err.cause, err.retry_after) == ("rate_limited", 7.0)


def test_500():
    assert failure(groq(lambda r: httpx.Response(500, text="boom"))).cause == "server_error_500"


def test_other_non_200_reports_groq_error_code_only():
    body = {"error": {"code": "json_validate_failed", "message": "long text " * 50, "failed_generation": "x"}}
    assert failure(groq(lambda r: httpx.Response(400, json=body))).cause == "http_400: json_validate_failed"


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    assert failure(groq(handler)).cause == "timeout"


def test_connection_error():
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    assert failure(groq(handler)).cause == "connection: ConnectError"


@pytest.mark.parametrize("response,cause", [
    (httpx.Response(200, text="<html>not json</html>"), "bad_body"),
    (httpx.Response(200, json={"choices": []}), "bad_body"),
    (httpx.Response(200, json={"no_choices": True}), "bad_body"),
    (httpx.Response(200, json=ok_body("")), "empty"),
    (httpx.Response(200, json=ok_body("   ")), "empty"),
], ids=["html_body", "empty_choices", "missing_choices", "empty_content", "blank_content"])
def test_bad_bodies(response, cause):
    assert failure(groq(lambda r: response)).cause == cause


# --- call budget ------------------------------------------------------------------------------
def test_budget_allows_two_and_raises_on_third():
    budget = CallBudget()
    budget.spend()
    budget.spend()
    with pytest.raises(CallBudgetExceeded):
        budget.spend()
    assert budget.used == 3


# --- FakeLLM ----------------------------------------------------------------------------------
def test_fake_extractive_answers_with_s1():
    out = json.loads(FakeLLM().complete(PROMPT))
    assert out == {"status": "answered", "answer": "Section 4 Night\nAfter 9:00 PM.", "citations": ["S1"]}


def test_fake_without_excerpts_is_not_covered():
    empty = Prompt(system="", user="", excerpts=[], question="q")
    assert json.loads(FakeLLM().complete(empty))["status"] == "not_covered"


@pytest.mark.parametrize("mode,citations", [("bad_citation", ["S9"]), ("too_many_citations", ["S1", "S2", "S3", "S4"]),
                                            ("answered_without_citation", [])])
def test_fake_citation_modes(mode, citations):
    assert json.loads(FakeLLM(mode).complete(PROMPT))["citations"] == citations


@pytest.mark.parametrize("mode,cause", [("timeout", "timeout"), ("error_429", "rate_limited")])
def test_fake_failure_modes(mode, cause):
    with pytest.raises(LLMUnavailable) as exc:
        FakeLLM(mode).complete(PROMPT)
    assert exc.value.cause == cause


def test_fake_malformed_mode_fails_parsing():
    with pytest.raises(LLMUnavailable, match="malformed_output"):
        parse_llm_output(FakeLLM("malformed").complete(PROMPT))


def test_fake_modes_are_exactly_the_documented_set():
    assert set(FAKE_MODES) == {"extractive", "malformed", "timeout", "error_429", "bad_citation",
                               "too_many_citations", "answered_without_citation"}
    with pytest.raises(ValueError):
        FakeLLM("smart")


# --- strict parsing ---------------------------------------------------------------------------
def test_parse_accepts_valid_and_ignores_unknown_keys():
    out = parse_llm_output(json.dumps({**GOOD, "reasoning": "S1 says after 9 PM", "confidence": 0.9}))
    assert (out.status, out.answer, out.citations) == ("answered", "After 9:00 PM.", ["S1"])
    assert not hasattr(out, "reasoning")


@pytest.mark.parametrize("raw", [
    "not json", "[1, 2]", "null",
    json.dumps({"answer": "x", "citations": []}),                                    # missing status
    json.dumps({"status": "maybe", "answer": "x", "citations": []}),                 # bad status
    json.dumps({"status": "answered", "answer": 42, "citations": ["S1"]}),           # answer not a string
    json.dumps({"status": "answered", "answer": "x", "citations": "S1"}),            # citations not a list
    json.dumps({"status": "answered", "answer": "x", "citations": [1]}),             # label not a string
    json.dumps({"status": "answered", "citations": ["S1"]}),                         # missing answer
], ids=["text", "array", "null", "no_status", "bad_status", "answer_int", "citations_str", "label_int", "no_answer"])
def test_parse_rejects_malformed(raw):
    with pytest.raises(LLMUnavailable, match="malformed_output"):
        parse_llm_output(raw)
