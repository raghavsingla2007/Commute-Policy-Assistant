"""LLM clients: GroqLLM (real), FakeLLM (deterministic, for tests and the no-key gate), call budget,
and strict parsing of the model's JSON.

Rules:
- One call per question on the normal path. CallBudget raises on a 3rd call (the brief allows 2).
- No retries inside a request: a retry can't fit in the 3 s budget (D12).
- Every failure becomes LLMUnavailable(cause). The cause is logged; the API key never is.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.schemas import LLMOutput

log = logging.getLogger("app.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMUnavailable(Exception):
    """The model could not give a usable reply. `cause` is a short code for logs and the eval."""

    def __init__(self, cause: str, retry_after: float | None = None) -> None:
        super().__init__(cause)
        self.cause = cause
        self.retry_after = retry_after


class CallBudgetExceeded(RuntimeError):
    pass


class CallBudget:
    """Per-request counter. The brief allows at most 2 LLM calls per question; we use 1."""

    MAX_CALLS = 2

    def __init__(self) -> None:
        self.used = 0

    def spend(self) -> None:
        self.used += 1
        if self.used > self.MAX_CALLS:
            raise CallBudgetExceeded(f"LLM call #{self.used} exceeds the budget of {self.MAX_CALLS}")


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    excerpts: list[tuple[str, str]]   # (label, chunk text) — FakeLLM reads these; GroqLLM sends system+user
    question: str


class LLM(Protocol):
    name: str

    def complete(self, prompt: Prompt) -> str:
        """Return the model's raw JSON text, or raise LLMUnavailable."""


# ---------------------------------------------------------------- Groq

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text.strip()


class GroqLLM:
    """Groq's OpenAI-compatible chat endpoint over plain httpx. JSON mode, temperature 0, one shot."""

    name = "groq"

    def __init__(self, api_key: str, model: str, timeout_s: float, max_tokens: int, reasoning_effort: str,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.model, self.max_tokens, self.reasoning_effort = model, max_tokens, reasoning_effort
        # One client for the process: connection reuse saves a TLS handshake per question.
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s), transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def __repr__(self) -> str:                      # never show the key
        return f"GroqLLM(model={self.model!r})"

    def close(self) -> None:
        self._client.close()

    def _body(self, prompt: Prompt) -> dict:
        body = {
            "model": self.model, "temperature": 0, "max_completion_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": prompt.system}, {"role": "user", "content": prompt.user}],
        }
        if self.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = self.reasoning_effort
            body["include_reasoning"] = False       # we never read or show the model's reasoning
        return body

    def complete(self, prompt: Prompt) -> str:
        try:
            resp = self._client.post(GROQ_URL, json=self._body(prompt))
        except httpx.TimeoutException:
            raise LLMUnavailable("timeout") from None
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"connection: {type(exc).__name__}") from None
        if resp.status_code == 429:
            retry_after = _float_or_none(resp.headers.get("retry-after"))
            raise LLMUnavailable("rate_limited", retry_after=retry_after)
        if resp.status_code >= 500:
            raise LLMUnavailable(f"server_error_{resp.status_code}")
        if resp.status_code != 200:
            raise LLMUnavailable(f"http_{resp.status_code}: {_error_code(resp)}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMUnavailable("bad_body") from None
        if not isinstance(content, str) or not content.strip():
            raise LLMUnavailable("empty")
        return strip_code_fences(content)


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_code(resp: httpx.Response) -> str:
    """Groq's short error code (e.g. json_validate_failed), never the whole body."""
    try:
        return str(resp.json()["error"].get("code") or "unknown")[:60]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unknown"


# ---------------------------------------------------------------- Fake

FAKE_MODES = ("extractive", "malformed", "timeout", "error_429", "bad_citation", "too_many_citations",
              "answered_without_citation")


class FakeLLM:
    """Deterministic stand-in (D24): answers with S1's text and cites S1; no excerpts -> not_covered.
    It is deliberately NOT smart: it cannot judge silence, so near-miss cases fail under it.
    The other modes exist only so tests can drive every failure path."""

    name = "fake"

    def __init__(self, mode: str = "extractive") -> None:
        if mode not in FAKE_MODES:
            raise ValueError(f"unknown FakeLLM mode {mode!r}")
        self.mode = mode

    def complete(self, prompt: Prompt) -> str:
        m = self.mode
        if m == "timeout":
            raise LLMUnavailable("timeout")
        if m == "error_429":
            raise LLMUnavailable("rate_limited", retry_after=1.0)
        if m == "malformed":
            return '{"status": "answered", "answer": '                 # truncated JSON
        if not prompt.excerpts:
            return json.dumps({"status": "not_covered", "answer": "", "citations": []})
        label, text = prompt.excerpts[0]
        citations = {"bad_citation": ["S9"], "too_many_citations": ["S1", "S2", "S3", "S4"],
                     "answered_without_citation": []}.get(m, [label])
        return json.dumps({"status": "answered", "answer": text, "citations": citations})


# ---------------------------------------------------------------- parsing + factory

def parse_llm_output(raw: str) -> LLMOutput:
    """Strict: valid JSON object with status/answer/citations of the right types. Else LLMUnavailable."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise LLMUnavailable("malformed_output: not JSON") from None
    if not isinstance(data, dict):
        raise LLMUnavailable("malformed_output: not an object")
    try:
        return LLMOutput.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise LLMUnavailable(f"malformed_output: bad fields {fields}") from None


def build_llm(settings) -> LLM:
    if settings.llm_mode == "groq":
        return GroqLLM(settings.groq_api_key, settings.llm_model, settings.llm_timeout_s,
                       settings.llm_max_tokens, settings.llm_reasoning_effort)
    return FakeLLM()
