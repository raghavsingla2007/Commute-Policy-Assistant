"""The /ask pipeline: the one file to read to understand a request.

    validate -> retrieve -> pre-filter -> prompt -> ONE LLM call -> parse -> citations -> verify -> render

answer() returns an AskResult. It raises only:
  - QuestionInvalid                      (P6 maps to 422)
  - RetrievalError and its subclasses    (P6 maps to 503 "try again")
  - LLMUnavailable                       (P6 maps to 503 "try again")
HTTP, request ids and the overall deadline are added in P6.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from app import messages
from app.auth import Principal
from app.config import Settings
from app.grounding import (CitationError, check_values, foreign_companies, render_citations, strip_labels,
                           validate_citations)
from app.llm import LLM, CallBudget, parse_llm_output
from app.prompts import SYSTEM_PROMPT, build_prompt
from app.retrieval import RetrievalResult, passes_prefilter, retrieve
from app.schemas import AskResult
from app.store import Store

log = logging.getLogger("app.ask")


class QuestionInvalid(ValueError):
    pass


@dataclass
class AskContext:
    store: Store
    llm: LLM
    settings: Settings
    system_prompt: str = SYSTEM_PROMPT     # the eval can A/B prompt versions (DEV only)


def validate_question(question: object, max_chars: int) -> str:
    if not isinstance(question, str) or not question.strip():
        raise QuestionInvalid("question must be non-empty text")
    if len(question) > max_chars:
        raise QuestionInvalid(f"question longer than {max_chars} characters")
    return question.strip()


def _not_covered(principal: Principal, r: RetrievalResult, reason: str, **extra) -> AskResult:
    return AskResult(status="not_covered", message=messages.NOT_COVERED, company_id=principal.company_id,
                     policy_version=r.version, reason=reason, retrieve_ms=r.retrieve_ms,
                     retrieved_sections=[c.section_id for c in r.chunks],
                     retrieved_companies=[c.company_id for c in r.chunks], **extra)


async def answer(principal: Principal, question: object, ctx: AskContext, request_id: str = "-") -> AskResult:
    q = validate_question(question, ctx.settings.max_question_chars)
    result = await _run(principal, q, ctx)
    log.info("ask request_id=%s company=%s version=%s status=%s reason=%s retrieve_ms=%.0f llm_ms=%.0f "
             "llm_calls=%d q=%r", request_id, principal.company_id, result.policy_version, result.status,
             result.reason, result.retrieve_ms, result.llm_ms, result.llm_calls, q[:100])
    return result


async def _run(principal: Principal, q: str, ctx: AskContext) -> AskResult:
    r = await retrieve(ctx.store, principal, q, ctx.settings)
    if r.version is None:
        return _not_covered(principal, r, "no_active_policy")
    if not passes_prefilter(r, ctx.settings.not_covered_min_sim):
        return _not_covered(principal, r, "below_threshold", detail=f"best similarity {r.best_similarity:.3f}")

    prompt = build_prompt([c.text for c in r.chunks], q, ctx.system_prompt)
    budget = CallBudget()
    budget.spend()                                          # call 1 of at most 2; we never make a second
    started = time.perf_counter()
    raw = await asyncio.to_thread(ctx.llm.complete, prompt)  # LLMUnavailable propagates
    llm_ms = (time.perf_counter() - started) * 1000
    out = parse_llm_output(raw)                              # malformed -> LLMUnavailable
    common = {"llm_calls": budget.used, "llm_ms": llm_ms}

    if out.status == "not_covered":
        return _not_covered(principal, r, "llm_not_covered", **common)
    try:
        labels = validate_citations(out.status, out.citations, len(r.chunks))
    except CitationError as exc:
        return _not_covered(principal, r, "bad_citation", detail=str(exc), **common)

    text = strip_labels(out.answer)                          # "" / whitespace / only "(S1)" -> nothing to show
    if not text:
        return _not_covered(principal, r, "unverified", cited_labels=labels, detail="empty answer", **common)

    cited = [r.chunks[int(lab[1:]) - 1] for lab in labels]
    problem = check_values(out.answer, [c.text for c in cited], q)
    foreign = foreign_companies(out.answer, principal.company_id)
    if problem or foreign:
        detail = problem or f"names other companies {foreign}"
        return _not_covered(principal, r, "unverified", cited_labels=labels, detail=detail, **common)

    return AskResult(status="answered", answer=text, citations=render_citations(labels, r.chunks),
                     company_id=principal.company_id, policy_version=r.version, cited_labels=labels,
                     retrieved_sections=[c.section_id for c in r.chunks],
                     retrieved_companies=[c.company_id for c in r.chunks], retrieve_ms=r.retrieve_ms, **common)
