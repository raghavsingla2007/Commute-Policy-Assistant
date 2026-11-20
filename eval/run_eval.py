"""Evaluation runner over the real HTTP path.

    python -m eval.run_eval [--llm fake|groq] [--split dev|heldout|all] [--retrieval vector|hybrid]
                            [--prompt v1|v1+reasoning] [--repeat N] [--min-interval S] [--tag T] [--quiet]

How it works (P6):
1. Sets its own random JWT_SECRET (D25) and a fresh temp DATA_DIR, then starts the real app with
   FastAPI's TestClient. Startup seeds v1 of all three companies.
2. Upgrades Kestrel to v2 THROUGH POST /admin/policies with an admin token, like an operator would.
3. Sends every case to POST /ask with a token minted in-process (or a malformed/expired one for auth
   cases). Latency is the client's wall clock around the HTTP call.
4. The API never returns internal reasons, so the app's eval hook (app.state.result_sink) hands the runner
   the internal outcome per request_id: reason, retrieved sections, llm_calls, or the failure cause.

Groq pacing: 8,000 tokens/minute on the free tier (~900 tokens per call), so --min-interval 8 s between
LLM calls by default. A 503 caused by a 429 is retried ONCE here, after Retry-After. Never inside a request.

Exit code: 0 whenever the run finishes (even if cases fail), non-zero only on a crash.
"""

import argparse
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import jwt

from app.config import PROJECT_ROOT
from app.grounding import unsupported_values
from app.ingest import parse_policy
from app.llm import LLMUnavailable
from app.prompts import PROMPTS
from app.schemas import AskResult
from eval.freeze import CASES, load_cases, split_of
from eval.metrics import percentile, retrieval_ranks, score_answer

FIELDS = ("id", "input", "expected", "actual", "pass", "notes")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
POLICIES = PROJECT_ROOT / "data" / "policies"
ACTIVE_FILE = {"kestrel": "kestrel_v2.md", "tamarind": "tamarind_v1.md", "orbitron": "orbitron_v1.md"}
CANARIES = {"kestrel": ["4411", "Falcon Fleet"], "tamarind": ["5523", "Monsoon Cabs"],
            "orbitron": ["7720", "Nimbus Rides"]}
MALFORMED_CAUSES = ("malformed_output", "bad_body", "empty", "truncated", "http_400: json_validate_failed")


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
    """Active policy text per company/section, straight from the files: an independent source for the
    unsupported-claim check (it does not trust the pipeline's own verifier)."""
    out = {}
    for company, name in ACTIVE_FILE.items():
        policy = parse_policy((POLICIES / name).read_text(encoding="utf-8"))
        out[company] = {s.section_id: f"Section {s.section_id} {s.title}\n{s.text}" for s in policy.sections}
    return out


def case_token(app, case: dict) -> str:
    from app.auth import mint_token

    company, kind = case["input"]["company"], case["input"].get("auth", "valid")
    if kind == "malformed":
        return "not.a.valid.jwt"
    if kind == "expired":
        s, now = app.state.settings, int(time.time())
        return jwt.encode({"iss": s.jwt_issuer, "aud": s.jwt_audience, "sub": "eval", "iat": now - 3600,
                           "exp": now - 600, "company_id": company, "role": "employee"}, s.jwt_secret, "HS256")
    return mint_token(app.state.settings, company, "employee", f"eval-{case['id']}")


def ask_http(client, app, case: dict, outcomes: dict) -> dict:
    headers = {"Authorization": f"Bearer {case_token(app, case)}", "X-Request-ID": f"eval-{case['id']}-{time.time_ns()}"}
    started = time.perf_counter()
    resp = client.post("/ask", headers=headers, json={"question": case["input"]["question"]})
    latency = (time.perf_counter() - started) * 1000
    body = resp.json()
    actual = {"http_status": resp.status_code, "status": body.get("status"),
              "answer": body.get("answer"), "message": body.get("message"),
              "citations": [c["section_id"] for c in body.get("citations", [])],
              "cited_sections": [c["section_id"] for c in body.get("citations", [])],
              "policy_version": body.get("policy_version"), "latency_ms": round(latency, 1),
              "retry_after": resp.headers.get("retry-after")}
    outcome = outcomes.pop(body.get("request_id"), None)
    if isinstance(outcome, AskResult):
        actual.update({"reason": outcome.reason, "detail": outcome.detail, "retrieved_sections": outcome.retrieved_sections,
                       "llm_calls": outcome.llm_calls, "retrieve_ms": round(outcome.retrieve_ms, 1),
                       "llm_ms": round(outcome.llm_ms, 1)})
    elif isinstance(outcome, LLMUnavailable):
        actual.update({"reason": "llm_unavailable", "llm_cause": outcome.cause, "llm_calls": 1, "retrieved_sections": []})
    elif outcome is not None:
        actual.update({"reason": type(outcome).__name__, "detail": str(outcome)[:200], "llm_calls": 0,
                       "retrieved_sections": []})
    else:                                    # 401/422: rejected before the pipeline ran
        actual.update({"reason": None, "llm_calls": 0, "retrieved_sections": []})
    if actual["status"] == "not_covered":    # the API's answer field holds the fixed message
        actual["message"], actual["answer"] = actual["answer"], None
    return actual


def check_case(case: dict, actual: dict, texts: dict) -> tuple[dict, bool]:
    company = case["input"]["company"]
    scores = score_answer(case, actual)
    shown = actual.get("answer") or actual.get("message") or ""
    foreign = [c for other, cs in CANARIES.items() if other != company for c in cs if c in shown]
    unsupported = []
    if actual["status"] == "answered":
        sources = [texts[company][s] for s in actual["cited_sections"]] + [case["input"]["question"]]
        unsupported = unsupported_values(actual["answer"], sources)
    rank = retrieval_ranks(actual.get("retrieved_sections", []), case["expected"]["sections"],
                           case["expected"].get("all_sections", False))
    scores.update({"leaks": foreign, "unsupported": unsupported, "retrieval_rank": rank})
    return scores, scores["pass"] and not foreign and not unsupported


def summarise(records: list[dict], latencies: list[float], n429: int) -> dict:
    a = [r["actual"] for r in records]
    by_kind = {k: [r for r in records if kind_of(r) == k] for k in ("answerable", "not_covered", "either", "auth")}
    answered_ok = [r for r in by_kind["answerable"] if r["actual"]["status"] == "answered"]
    ranks = [r["actual"]["scores"]["retrieval_rank"] for r in by_kind["answerable"]]
    false_refusals = [r for r in by_kind["answerable"] if r["actual"]["status"] == "not_covered"]
    reasons = {}
    for r in false_refusals:
        reasons[r["actual"]["reason"]] = reasons.get(r["actual"]["reason"], 0) + 1
    rate = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    return {
        "cases": len(records), "pass": sum(r["pass"] for r in records), "pass_rate": rate([r["pass"] for r in records]),
        "status_accuracy": rate([x["scores"]["status_ok"] for x in a]),
        "false_refusals": len(false_refusals), "false_refusal_reasons": reasons,
        "not_covered_accuracy": rate([r["actual"]["status"] == "not_covered" for r in by_kind["not_covered"]]),
        "must_include_pass_rate": rate([r["actual"]["scores"]["include_ok"] for r in answered_ok]),
        "must_not_pass_rate": rate([x["scores"]["exclude_ok"] for x in a]),
        "citation_correct_rate": rate([r["actual"]["scores"]["citation_ok"] for r in answered_ok]),
        "auth_cases_correct": sum(r["actual"]["http_status"] == 401 for r in by_kind["auth"]),
        "auth_cases": len(by_kind["auth"]),
        "unsupported_claims": sum(bool(x["scores"]["unsupported"]) for x in a),
        "leaks": sum(bool(x["scores"]["leaks"]) for x in a),
        "verifier_rejections": sum(x.get("reason") == "unverified" for x in a),
        "bad_citations": sum(x.get("reason") == "bad_citation" for x in a),
        "below_threshold": sum(x.get("reason") == "below_threshold" for x in a),
        "malformed_outputs": sum(str(x.get("llm_cause", "")).startswith(MALFORMED_CAUSES) for x in a),
        "unavailable_503": sum(x["http_status"] == 503 for x in a),
        "llm_calls_max": max((x.get("llm_calls", 0) for x in a), default=0),
        "http_429": n429,
        "hit@1": rate([x == 1 for x in ranks]), "hit@3": rate([x is not None and x <= 3 for x in ranks]),
        "latency_ms": {"p50": round(percentile(latencies, 50), 1) if latencies else None,
                       "p95": round(percentile(latencies, 95), 1) if latencies else None,
                       "max": round(max(latencies), 1) if latencies else None, "n": len(latencies)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set through POST /ask")
    parser.add_argument("--llm", choices=["fake", "groq"], default="fake")
    parser.add_argument("--retrieval", choices=["vector", "hybrid"], default="vector")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default="v1")
    parser.add_argument("--repeat", type=int, default=1, help="ask each case N times (latency; pass needs all N)")
    parser.add_argument("--min-interval", type=float, default=8.0, help="seconds between Groq calls")
    parser.add_argument("--tag", default="", help="suffix for the output file name")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    # Closed system: own secret, own data dir, DEV_MODE off. Set BEFORE the app reads its settings.
    tmp = tempfile.TemporaryDirectory()
    os.environ.update({"JWT_SECRET": secrets.token_urlsafe(48), "DATA_DIR": str(Path(tmp.name) / "data"),
                       "DEV_MODE": "false", "LLM_PROVIDER": args.llm, "RETRIEVAL_MODE": args.retrieval})
    if args.llm == "groq" and not os.environ.get("GROQ_API_KEY"):
        print("error: --llm groq needs GROQ_API_KEY (in .env, passed by docker compose)")
        return 2

    import logging

    from fastapi.testclient import TestClient

    from app.auth import mint_token
    from app.main import app

    for name in ("app", "httpx", "httpx2"):  # per-request INFO lines would drown the table; warnings still show
        logging.getLogger(name).setLevel(logging.WARNING)

    cases = [c for c in load_cases(CASES) if args.split == "all" or split_of(c) == args.split]
    texts = section_texts()
    records, latencies, n429 = [], [], 0
    outcomes: dict = {}
    with TestClient(app) as client:
        assert app.state.settings.llm_mode == args.llm, f"app is using {app.state.settings.llm_mode}, not {args.llm}"
        admin = {"Authorization": f"Bearer {mint_token(app.state.settings, 'kestrel', 'admin', 'eval-admin')}"}
        up = client.post("/admin/policies", headers=admin,
                         json={"markdown": (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8")})
        if up.status_code != 201:
            raise RuntimeError(f"Kestrel v2 upgrade through the admin API failed: {up.status_code} {up.text}")
        print(f"upgraded via POST /admin/policies: {json.loads(up.text) | {'request_id': '…'}}")
        app.state.system_prompt = PROMPTS[args.prompt]
        app.state.result_sink = outcomes.__setitem__
        last_llm_call = 0.0
        for case in cases:
            runs = []
            for _ in range(args.repeat):
                if args.llm == "groq" and kind_of(case) != "auth":
                    wait = args.min_interval - (time.monotonic() - last_llm_call)
                    if wait > 0:
                        time.sleep(wait)
                actual = ask_http(client, app, case, outcomes)
                if actual.get("llm_cause") == "rate_limited":
                    n429 += 1
                    time.sleep(float(actual.get("retry_after") or 10))
                    actual = ask_http(client, app, case, outcomes)              # one runner-level retry
                    n429 += actual.get("llm_cause") == "rate_limited"
                if actual.get("llm_calls"):
                    last_llm_call = time.monotonic()
                if actual["http_status"] == 200:
                    latencies.append(actual["latency_ms"])
                scores, ok = check_case(case, actual, texts)
                actual["scores"] = scores
                runs.append((actual, ok))
            actual, ok = runs[0]
            ok = all(o for _, o in runs)
            if args.repeat > 1:
                actual["repeats"] = [{"status": a["status"], "pass": o, "latency_ms": a["latency_ms"]} for a, o in runs]
            s = actual["scores"]
            why = "pass" if ok else "FAIL: " + ", ".join(
                k for k, v in [("status", s["status_ok"]), ("must_include", s["include_ok"]),
                               ("must_not_include", s["exclude_ok"]), ("citation", s["citation_ok"]),
                               ("leak", not s["leaks"]), ("unsupported", not s["unsupported"]),
                               ("repeat", all(o for _, o in runs))] if not v)
            record = {**case, "actual": actual, "pass": ok,
                      "notes": f"{case['notes']} | {args.llm}/{args.prompt}/http: {why}"
                               + (f" (reason={actual.get('reason')})" if actual.get("reason") else "")}
            assert tuple(record) == FIELDS
            records.append(record)
    tmp.cleanup()

    summary = summarise(records, latencies, n429)
    summary.update({"llm": args.llm, "model": os.environ.get("LLM_MODEL", "openai/gpt-oss-20b") if args.llm == "groq" else "fake",
                    "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "300")), "prompt": args.prompt,
                    "retrieval": args.retrieval, "split": args.split, "repeat": args.repeat, "path": "http"})
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{args.llm}_{args.retrieval}_{args.split}" + (f"_{args.tag}" if args.tag else "")
    out = RESULTS_DIR / f"{stem}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    (RESULTS_DIR / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.quiet:
        for r in records:
            a = r["actual"]
            text = (a.get("answer") or a.get("llm_cause") or a.get("detail") or a.get("message") or "")[:88]
            print(f"{r['id']:22} {'PASS' if r['pass'] else 'FAIL'} {a['http_status']} {a['status']:11} "
                  f"{str(a.get('reason') or ''):15} cited={','.join(a['citations']) or '-':6} {a['latency_ms']:6.0f}ms  "
                  + text.replace("\n", " "))
    print(json.dumps(summary))
    print(f"wrote {len(records)} cases to eval/results/{out.name} (+ .summary.json)")
    if args.llm == "fake":
        print("!" * 70)
        print("FakeLLM run: pipeline check only; quality metrics need --llm groq")
        print("!" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
