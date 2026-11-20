"""Evaluation runner.

    python -m eval.run_eval [--llm fake|groq] [--split dev|heldout|all] [--retrieval vector|hybrid]
                            [--prompt v1|v1+reasoning] [--min-interval S] [--quiet]

P5: every case goes through the real orchestrator `app.ask.answer()` in-process (P6 switches this to
the HTTP path through TestClient). It builds its own fresh store in a temp dir (seed v1 x3, then
Kestrel v2), so results never depend on the server's data.

Groq pacing: the free tier allows 8,000 tokens/minute for openai/gpt-oss-20b (one call is ~900 tokens),
so the runner waits --min-interval seconds (default 8) between LLM-calling cases. A 429 is retried ONCE
here, at runner level, after Retry-After. Never inside a request (D12). 429s are counted and reported.

Exit code: 0 whenever the run finishes (even if cases fail), non-zero only on a crash.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from app.ask import AskContext, answer
from app.auth import Principal
from app.config import PROJECT_ROOT, load_settings
from app.embeddings import shared_embedder
from app.grounding import unsupported_values
from app.ingest import ingest_policy, parse_policy, seed_if_empty
from app.llm import FakeLLM, GroqLLM, LLMUnavailable
from app.prompts import PROMPTS
from app.retrieval import RetrievalError
from app.store import Store
from eval.freeze import CASES, load_cases, split_of
from eval.metrics import percentile, retrieval_ranks, score_answer

FIELDS = ("id", "input", "expected", "actual", "pass", "notes")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
POLICIES = PROJECT_ROOT / "data" / "policies"
ACTIVE_FILE = {"kestrel": "kestrel_v2.md", "tamarind": "tamarind_v1.md", "orbitron": "orbitron_v1.md"}
CANARIES = {"kestrel": ["4411", "Falcon Fleet"], "tamarind": ["5523", "Monsoon Cabs"],
            "orbitron": ["7720", "Nimbus Rides"]}
MALFORMED_CAUSES = ("malformed_output", "bad_body", "empty", "http_400: json_validate_failed")


def kind_of(case: dict) -> str:
    status = case["expected"]["status"]
    if case["input"].get("auth", "valid") != "valid":
        return "auth"
    if status == ["answered"]:
        return "answerable"
    if status == ["not_covered"]:
        return "not_covered"
    return "either"


def section_texts() -> dict[str, dict[str, str]]:
    """Active policy text per company/section, read straight from the files: an independent source
    for the unsupported-claim check (does not trust the pipeline's own verifier)."""
    out = {}
    for company, name in ACTIVE_FILE.items():
        policy = parse_policy((POLICIES / name).read_text(encoding="utf-8"))
        out[company] = {s.section_id: f"Section {s.section_id} {s.title}\n{s.text}" for s in policy.sections}
    return out


def build_store(data_dir: Path) -> Store:
    store = Store(data_dir, shared_embedder())
    seed_if_empty(store, POLICIES)
    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    store.warm()
    return store


async def ask_once(ctx: AskContext, case: dict) -> tuple[dict, float]:
    principal = Principal(case["input"]["company"], "employee", f"eval-{case['id']}")
    started = time.perf_counter()
    try:
        res = await answer(principal, case["input"]["question"], ctx)
        actual = {
            "status": res.status, "answer": res.answer, "message": res.message,
            "citations": [c.section_id for c in res.citations], "cited_sections": [c.section_id for c in res.citations],
            "retrieved_sections": res.retrieved_sections, "reason": res.reason, "detail": res.detail,
            "llm_calls": res.llm_calls, "retrieve_ms": round(res.retrieve_ms, 1), "llm_ms": round(res.llm_ms, 1),
            "policy_version": res.policy_version,
        }
    except LLMUnavailable as exc:
        actual = {"status": "unavailable", "reason": "llm_unavailable", "llm_cause": exc.cause,
                  "retry_after": exc.retry_after, "llm_calls": 1, "citations": [], "retrieved_sections": []}
    except RetrievalError as exc:
        actual = {"status": "unavailable", "reason": "retrieval_error", "detail": str(exc), "llm_calls": 0,
                  "citations": [], "retrieved_sections": []}
    elapsed = (time.perf_counter() - started) * 1000
    actual["latency_ms"] = round(elapsed, 1)
    return actual, elapsed


def check_case(case: dict, actual: dict, texts: dict) -> tuple[dict, bool]:
    company = case["input"]["company"]
    scores = score_answer(case, actual)
    shown = actual.get("answer") or actual.get("message") or ""
    foreign = [c for other, cs in CANARIES.items() if other != company for c in cs if c in shown]
    unsupported = []
    if actual["status"] == "answered":
        sources = [texts[company][s] for s in actual["cited_sections"]] + [case["input"]["question"]]
        unsupported = unsupported_values(actual["answer"], sources)
    exp_sections = case["expected"]["sections"]
    rank = retrieval_ranks(actual.get("retrieved_sections", []), exp_sections, case["expected"].get("all_sections", False))
    scores.update({"leaks": foreign, "unsupported": unsupported, "retrieval_rank": rank})
    ok = scores["pass"] and not foreign and not unsupported
    return scores, ok


def summarise(records: list[dict], latencies: list[float], n429: int) -> dict:
    scored = [r for r in records if kind_of(r) != "auth"]
    a = [r["actual"] for r in scored]
    by_kind = {k: [r for r in scored if kind_of(r) == k] for k in ("answerable", "not_covered", "either")}
    answered_ok = [r for r in by_kind["answerable"] if r["actual"]["status"] == "answered"]
    ranks = [r["actual"]["scores"]["retrieval_rank"] for r in by_kind["answerable"]]
    false_refusals = [r for r in by_kind["answerable"] if r["actual"]["status"] == "not_covered"]
    reasons = {}
    for r in false_refusals:
        reasons[r["actual"]["reason"]] = reasons.get(r["actual"]["reason"], 0) + 1
    rate = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    return {
        "cases_scored": len(scored), "pass": sum(r["pass"] for r in scored),
        "pass_rate": rate([r["pass"] for r in scored]),
        "status_accuracy": rate([x["scores"]["status_ok"] for x in a]),
        "false_refusals": len(false_refusals), "false_refusal_reasons": reasons,
        "not_covered_accuracy": rate([r["actual"]["status"] == "not_covered" for r in by_kind["not_covered"]]),
        "must_include_pass_rate": rate([r["actual"]["scores"]["include_ok"] for r in answered_ok]),
        "must_not_pass_rate": rate([x["scores"]["exclude_ok"] for x in a]),
        "citation_correct_rate": rate([r["actual"]["scores"]["citation_ok"] for r in answered_ok]),
        "unsupported_claims": sum(bool(x["scores"]["unsupported"]) for x in a),
        "leaks": sum(bool(x["scores"]["leaks"]) for x in a),
        "verifier_rejections": sum(x.get("reason") == "unverified" for x in a),
        "bad_citations": sum(x.get("reason") == "bad_citation" for x in a),
        "below_threshold": sum(x.get("reason") == "below_threshold" for x in a),
        "malformed_outputs": sum(str(x.get("llm_cause", "")).startswith(MALFORMED_CAUSES) for x in a),
        "unavailable": sum(x["status"] == "unavailable" for x in a),
        "llm_calls_max": max((x["llm_calls"] for x in a), default=0),
        "http_429": n429,
        "hit@1": rate([x == 1 for x in ranks]), "hit@3": rate([x is not None and x <= 3 for x in ranks]),
        "latency_ms": {"p50": round(percentile(latencies, 50), 1) if latencies else None,
                       "p95": round(percentile(latencies, 95), 1) if latencies else None,
                       "max": round(max(latencies), 1) if latencies else None, "n": len(latencies)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set through answer()")
    parser.add_argument("--llm", choices=["fake", "groq"], default="fake")
    parser.add_argument("--retrieval", choices=["vector", "hybrid"], default="vector")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default="v1")
    parser.add_argument("--min-interval", type=float, default=8.0, help="seconds between Groq calls")
    parser.add_argument("--tag", default="", help="suffix for the output file name")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    # Closed system: own random JWT secret per run, independent of the server's config.
    os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
    settings = dataclasses.replace(load_settings(), retrieval_mode=args.retrieval)
    settings.validate()
    if args.llm == "groq":
        if not settings.groq_api_key:
            print("error: --llm groq needs GROQ_API_KEY (in .env, passed by docker compose)")
            return 2
        llm = GroqLLM(settings.groq_api_key, settings.llm_model, settings.llm_timeout_s,
                      settings.llm_max_tokens, settings.llm_reasoning_effort)
    else:
        llm = FakeLLM()

    cases = [c for c in load_cases(CASES) if args.split == "all" or split_of(c) == args.split]
    texts = section_texts()
    records, latencies, n429 = [], [], 0
    with tempfile.TemporaryDirectory() as tmp:
        store = build_store(Path(tmp) / "data")
        ctx = AskContext(store, llm, settings, PROMPTS[args.prompt])
        last_llm_call = 0.0
        try:
            for case in cases:
                if kind_of(case) == "auth":
                    actual = {"skipped": "auth case: needs the HTTP path (P6)"}
                    records.append({**case, "actual": actual, "pass": False,
                                    "notes": f"{case['notes']} | not run in P5 (in-process)"})
                    continue
                if args.llm == "groq":
                    wait = args.min_interval - (time.monotonic() - last_llm_call)
                    if wait > 0:
                        time.sleep(wait)
                actual, elapsed = asyncio.run(ask_once(ctx, case))
                if actual.get("llm_cause") == "rate_limited":
                    n429 += 1
                    time.sleep(actual.get("retry_after") or 10.0)
                    actual, elapsed = asyncio.run(ask_once(ctx, case))       # one runner-level retry
                    n429 += actual.get("llm_cause") == "rate_limited"
                if actual.get("llm_calls"):
                    last_llm_call = time.monotonic()
                if actual["status"] != "unavailable":
                    latencies.append(elapsed)
                scores, ok = check_case(case, actual, texts)
                actual["scores"] = scores
                why = "pass" if ok else "FAIL: " + ", ".join(
                    k for k, v in [("status", scores["status_ok"]), ("must_include", scores["include_ok"]),
                                   ("must_not_include", scores["exclude_ok"]), ("citation", scores["citation_ok"]),
                                   ("leak", not scores["leaks"]), ("unsupported", not scores["unsupported"])] if not v)
                record = {**case, "actual": actual, "pass": ok,
                          "notes": f"{case['notes']} | {args.llm}/{args.prompt}: {why}"
                                   + (f" (reason={actual.get('reason')})" if actual.get("reason") else "")}
                assert tuple(record) == FIELDS
                records.append(record)
        finally:
            store.close()
            if hasattr(llm, "close"):
                llm.close()

    summary = summarise(records, latencies, n429)
    summary.update({"llm": args.llm, "model": settings.llm_model if args.llm == "groq" else "fake",
                    "prompt": args.prompt, "retrieval": args.retrieval, "split": args.split,
                    "threshold": settings.not_covered_min_sim})
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.llm}_{args.retrieval}_{args.split}" + (f"_{args.tag}" if args.tag else "")
    out = RESULTS_DIR / f"{stem}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    (RESULTS_DIR / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.quiet:
        for r in records:
            a = r["actual"]
            if "skipped" in a:
                print(f"{r['id']:22} SKIP  {a['skipped']}")
                continue
            text = (a.get("answer") or a.get("llm_cause") or a.get("detail") or "")[:90].replace("\n", " ")
            print(f"{r['id']:22} {'PASS' if r['pass'] else 'FAIL'}  {a['status']:11} {str(a.get('reason') or ''):15} "
                  f"cited={','.join(a.get('citations', [])) or '-':6} {a['latency_ms']:7.0f}ms  {text}")
    print(json.dumps(summary))
    print(f"wrote {len(records)} cases to eval/results/{out.name} (+ .summary.json)")
    if args.llm == "fake":
        print("!" * 70)
        print("FakeLLM run: pipeline check only; quality metrics need --llm groq")
        print("!" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
