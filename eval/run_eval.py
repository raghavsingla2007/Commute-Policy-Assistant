"""Evaluation runner over the real HTTP path.

    python -m eval.run_eval [--llm fake|groq] [--split dev|heldout|all] [--retrieval vector|hybrid]
                            [--prompt v1|v1+reasoning] [--repeat N] [--min-interval S] [--max-429-rate R]
                            [--name STEM | --tag T] [--quiet]
    python -m eval.run_eval --export eval/results/<run>.jsonl [--export-to eval/eval_set.jsonl]
    python -m eval.run_eval --quota          # one tiny Groq call; prints the remaining daily requests

How a run works:
1. Own random JWT_SECRET (D25), fresh temp DATA_DIR, DEV_MODE off; the real app runs under FastAPI's
   TestClient. Startup seeds v1 of all three companies.
2. Kestrel is upgraded to v2 THROUGH POST /admin/policies with an admin token.
3. Every case goes to POST /ask with an in-process token (or a malformed/expired one for auth cases).
   Latency = client wall clock around the HTTP call; pauses between calls are not timed.
4. The API never returns internal reasons, so the app's eval hook (app.state.result_sink) gives the runner
   the internal outcome per request_id: reason, retrieved sections and their companies, llm_calls, causes.

Groq pacing: 8,000 tokens/minute on the free tier (~900 tokens per call) -> --min-interval 8 s between LLM
calls. A 503 caused by a 429 is retried ONCE here after Retry-After (never inside a request). If 429s exceed
--max-429-rate of LLM calls (after 20 calls), the run aborts with exit code 3 and writes what it has.

Exit code: 0 whenever the run finishes (even if cases fail); 3 on a 429 abort; other non-zero only on a crash.
"""

import argparse
import json
import os
import secrets
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import jwt

from app.config import PROJECT_ROOT
from app.grounding import unsupported_values
from app.ingest import parse_policy
from app.llm import LLMUnavailable
from app.prompts import PROMPTS
from app.schemas import AskResult
from eval.freeze import CASES, load_cases, split_of
from eval.metrics import retrieval_ranks, score_answer

FIELDS = ("id", "input", "expected", "actual", "pass", "notes")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DELIVERABLE = Path(__file__).resolve().parent / "eval_set.jsonl"
POLICIES = PROJECT_ROOT / "data" / "policies"
ACTIVE_FILE = {"kestrel": "kestrel_v2.md", "tamarind": "tamarind_v1.md", "orbitron": "orbitron_v1.md"}
CANARIES = {"kestrel": ["4411", "Falcon Fleet"], "tamarind": ["5523", "Monsoon Cabs"],
            "orbitron": ["7720", "Nimbus Rides"]}
MALFORMED_CAUSES = ("malformed_output", "bad_body", "empty", "truncated", "http_400: json_validate_failed")


# ---------------------------------------------------------------- per-case helpers

def kind_of(case: dict) -> str:
    status = case["expected"]["status"]
    if case["input"].get("auth", "valid") != "valid":
        return "auth"
    if status == ["answered"]:
        return "answerable"
    if status == ["not_covered"]:
        return "not_covered"
    return "either"


def categories_of(case: dict) -> list[str]:
    return case["notes"].split(";")[0].removeprefix("category=").split(",")


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
    headers = {"Authorization": f"Bearer {case_token(app, case)}",
               "X-Request-ID": f"eval-{case['id']}-{time.time_ns()}"[:64]}
    started = time.perf_counter()
    resp = client.post("/ask", headers=headers, json={"question": case["input"]["question"]})
    latency = (time.perf_counter() - started) * 1000
    body = resp.json()
    citations = body.get("citations", [])
    actual = {"http_status": resp.status_code, "status": body.get("status"),
              "answer": body.get("answer"), "message": body.get("message"),
              "citations": [c["section_id"] for c in citations], "cited_sections": [c["section_id"] for c in citations],
              "cited_doc_ids": [c["doc_id"] for c in citations],
              "policy_version": body.get("policy_version"), "latency_ms": round(latency, 1),
              "retry_after": resp.headers.get("retry-after")}
    outcome = outcomes.pop(body.get("request_id"), None)
    if isinstance(outcome, AskResult):
        actual.update({"reason": outcome.reason, "detail": outcome.detail,
                       "retrieved_sections": outcome.retrieved_sections,
                       "retrieved_companies": outcome.retrieved_companies,
                       "llm_calls": outcome.llm_calls, "retrieve_ms": round(outcome.retrieve_ms, 1),
                       "llm_ms": round(outcome.llm_ms, 1)})
    elif isinstance(outcome, LLMUnavailable):
        actual.update({"reason": "llm_unavailable", "llm_cause": outcome.cause, "llm_calls": 1})
    elif outcome is not None:
        actual.update({"reason": type(outcome).__name__, "detail": str(outcome)[:200], "llm_calls": 0})
    else:                                    # 401/422: rejected before the pipeline ran
        actual.update({"reason": None, "llm_calls": 0})
    actual.setdefault("retrieved_sections", [])
    actual.setdefault("retrieved_companies", [])
    if actual["status"] == "not_covered":    # the API's answer field holds the fixed message
        actual["message"], actual["answer"] = actual["answer"], None
    return actual


def find_leaks(company: str, actual: dict) -> list[str]:
    """§8.2: a foreign chunk retrieved or cited, or a foreign canary in the text the user sees."""
    shown = actual.get("answer") or actual.get("message") or ""
    leaks = [f"canary:{c}" for other, cs in CANARIES.items() if other != company for c in cs if c in shown]
    leaks += [f"retrieved:{c}" for c in actual.get("retrieved_companies", []) if c != company]
    # doc ids are "<company>-...": kestrel-transport-policy, tamarind-commute-policy, orbitron-transport-guidelines
    leaks += [f"cited:{d}" for d in actual.get("cited_doc_ids", []) if d.split("-")[0] != company]
    return leaks


def check_case(case: dict, actual: dict, texts: dict) -> tuple[dict, bool]:
    company = case["input"]["company"]
    scores = score_answer(case, actual)
    unsupported = []
    if actual["status"] == "answered":
        sources = [texts[company].get(s, "") for s in actual["cited_sections"]] + [case["input"]["question"]]
        unsupported = unsupported_values(actual["answer"], sources)
    rank = retrieval_ranks(actual.get("retrieved_sections", []), case["expected"]["sections"],
                           case["expected"].get("all_sections", False))
    leaks = find_leaks(company, actual)
    confidently_wrong = actual["status"] == "answered" and (not scores["include_ok"] or not scores["exclude_ok"])
    scores.update({"leaks": leaks, "unsupported": unsupported, "retrieval_rank": rank,
                   "confidently_wrong": confidently_wrong})
    return scores, scores["pass"] and not leaks and not unsupported


# ---------------------------------------------------------------- summaries

def p95(values: list[float]) -> float | None:
    """As planned in §8.1: statistics.quantiles(n=20)[18] (default 'exclusive' method)."""
    if not values:
        return None
    return values[0] if len(values) == 1 else statistics.quantiles(values, n=20)[18]


def p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def frac(xs: list[bool]) -> str | None:
    return f"{sum(xs)}/{len(xs)}" if xs else None


def rnd(x):
    return round(x, 1) if x is not None else None


def summarise(records: list[dict]) -> dict:
    a = [r["actual"] for r in records]
    by_kind = {k: [r for r in records if kind_of(r) == k] for k in ("answerable", "not_covered", "either", "auth")}
    answerable_answered = [r for r in by_kind["answerable"] if r["actual"]["status"] == "answered"]
    answered = [r for r in records if r["actual"]["status"] == "answered"]
    ranks = [r["actual"]["scores"]["retrieval_rank"] for r in by_kind["answerable"]]
    false_refusals = [r for r in by_kind["answerable"] if r["actual"]["status"] == "not_covered"]
    by_reason: dict[str, int] = {}
    for r in false_refusals:
        by_reason[r["actual"]["reason"]] = by_reason.get(r["actual"]["reason"], 0) + 1
    rejections: dict[str, int] = {}
    for x in a:
        if x.get("reason") == "unverified":
            key = (x.get("detail") or "").split(" [")[0].split(" ['")[0]
            rejections[key] = rejections.get(key, 0) + 1
        elif x.get("reason") == "bad_citation":
            rejections["bad_citation"] = rejections.get("bad_citation", 0) + 1
    return {
        "cases": len(records),
        "pass": frac([r["pass"] for r in records]),
        "status_accuracy": frac([x["scores"]["status_ok"] for x in a]),
        "not_covered_accuracy": frac([r["actual"]["status"] == "not_covered" for r in by_kind["not_covered"]]),
        "false_refusals": f"{len(false_refusals)}/{len(by_kind['answerable'])}", "false_refusal_reasons": by_reason,
        "answered_when_not_covered_expected": sum(r["actual"]["status"] == "answered" for r in by_kind["not_covered"]),
        "confidently_wrong": sum(x["scores"]["confidently_wrong"] for x in a),
        "must_include": frac([r["actual"]["scores"]["include_ok"] for r in answerable_answered]),
        "must_not_include": frac([x["scores"]["exclude_ok"] for x in a]),
        "citation_correct": frac([r["actual"]["scores"]["citation_ok"] for r in answered if r["expected"]["sections"]]),
        "auth": frac([r["actual"]["http_status"] == 401 for r in by_kind["auth"]]),
        "unsupported_claims": sum(bool(x["scores"]["unsupported"]) for x in a),
        "leaks": sum(bool(x["scores"]["leaks"]) for x in a),
        "verifier_rejections": rejections,
        "below_threshold": sum(x.get("reason") == "below_threshold" for x in a),
        "malformed_outputs": sum(str(x.get("llm_cause", "")).startswith(MALFORMED_CAUSES) for x in a),
        "truncations": sum(x.get("llm_cause") == "truncated" for x in a),
        "unavailable_503": sum(x["http_status"] == 503 for x in a),
        "llm_calls_max": max((x.get("llm_calls", 0) for x in a), default=0),
        "hit@1": frac([x == 1 for x in ranks]), "hit@3": frac([x is not None and x <= 3 for x in ranks]),
        "mrr": round(sum(1 / x for x in ranks if x) / len(ranks), 3) if ranks else None,
    }


def per_category(records: list[dict]) -> dict:
    cats: dict[str, list[bool]] = {}
    for r in records:
        for c in categories_of(r):
            cats.setdefault(c, []).append(r["pass"])
    return {c: frac(v) for c, v in sorted(cats.items())}


def latency_block(samples: list[dict]) -> dict:
    """samples: every completed HTTP call (all repeats). Client wall clock, plus per-stage numbers."""
    ok = [s for s in samples if s["http_status"] == 200]
    wall = [s["latency_ms"] for s in ok]
    retr = [s["retrieve_ms"] for s in ok if s.get("retrieve_ms") is not None]
    llm = [s["llm_ms"] for s in ok if s.get("llm_calls")]
    return {
        "samples_200": len(wall), "samples_total": len(samples),
        "wall_ms": {"p50": rnd(p50(wall)), "p95": rnd(p95(wall)), "max": rnd(max(wall)) if wall else None},
        "retrieve_ms": {"p50": rnd(p50(retr)), "p95": rnd(p95(retr))},
        "llm_ms": {"p50": rnd(p50(llm)), "p95": rnd(p95(llm)), "n": len(llm)},
        "p95_meets_3s": (p95(wall) is not None and p95(wall) <= 3000),
        "count_503": sum(s["http_status"] == 503 for s in samples),
        "count_timeouts": sum(s.get("llm_cause") == "timeout" or s.get("reason") == "TimeoutError" for s in samples),
    }


def determinism(records: list[dict]) -> dict:
    flips = []
    for r in records:
        reps = r["actual"].get("repeats")
        if not reps:
            continue
        if len({x["status"] for x in reps}) > 1 or len({x["include_ok"] for x in reps}) > 1 \
                or len({x["pass"] for x in reps}) > 1:
            flips.append({"id": r["id"], "statuses": [x["status"] for x in reps], "pass": [x["pass"] for x in reps]})
    wording = sum(len({x.get("answer") for x in r["actual"].get("repeats", [])}) > 1 for r in records)
    return {"cases_with_repeats": sum(bool(r["actual"].get("repeats")) for r in records),
            "status_or_key_fact_flips": len(flips), "flipped": flips, "cases_with_different_wording": wording}


# ---------------------------------------------------------------- deliverable export and quota

def export_deliverable(result_file: Path, target: Path) -> int:
    records = [json.loads(line) for line in result_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in records:
        assert tuple(r) == FIELDS, f"{r.get('id')}: fields {list(r)}"
        assert r["actual"] is not None and isinstance(r["pass"], bool), r["id"]
    target.write_text("".join(json.dumps({k: r[k] for k in FIELDS}, ensure_ascii=False) + "\n" for r in records),
                      encoding="utf-8")
    print(f"exported {len(records)} records from {result_file} to {target}")
    return 0


def quota() -> int:
    """One tiny real call; prints Groq's rate-limit headers (never the key)."""
    import httpx

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        print("no GROQ_API_KEY")
        return 2
    model = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")
    resp = httpx.post("https://api.groq.com/openai/v1/chat/completions", timeout=10,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_completion_tokens": 50, "reasoning_effort": "low",
                            "messages": [{"role": "user", "content": "Say ok."}]})
    hdr = {k: v for k, v in resp.headers.items() if k.startswith("x-ratelimit") or k == "retry-after"}
    print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model,
                      "status": resp.status_code, **hdr}))
    return 0


def rescore(result_file: Path, stem: str) -> int:
    """Scorer fixes must not need new LLM calls: re-check the stored answers with the current scorer."""
    texts = section_texts()
    old = [json.loads(line) for line in result_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = []
    for r in old:
        actual = {k: v for k, v in r["actual"].items() if k != "scores"}
        scores, ok = check_case(r, actual, texts)
        actual["scores"] = scores
        base = r["notes"].split(" | ")[0]
        records.append({**{k: r[k] for k in ("id", "input", "expected")}, "actual": actual, "pass": ok,
                        "notes": f"{base} | rescored from {result_file.name}: {'pass' if ok else 'FAIL'}"})
    prev = json.loads(result_file.with_suffix(".summary.json").read_text(encoding="utf-8"))
    summary = {"run": {**prev["run"], "rescored_from": result_file.name},
               "all": summarise(records), "dev": summarise([r for r in records if split_of(r) == "dev"]),
               "heldout": summarise([r for r in records if split_of(r) == "heldout"]),
               "per_category": per_category(records), "latency": prev["latency"], "determinism": prev["determinism"]}
    (RESULTS_DIR / f"{stem}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                                encoding="utf-8")
    (RESULTS_DIR / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    changed = [r["id"] for r, o in zip(records, old) if r["pass"] != o["pass"]]
    for key in ("all", "dev", "heldout", "per_category"):
        print(f"{key}: {json.dumps(summary[key], ensure_ascii=False)}")
    print(f"rescored {len(records)} records -> eval/results/{stem}.jsonl; pass changed for: {changed}")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set through POST /ask")
    parser.add_argument("--llm", choices=["fake", "groq"], default="fake")
    parser.add_argument("--retrieval", choices=["vector", "hybrid"], default="vector")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default="v1")
    parser.add_argument("--repeat", type=int, default=1, help="ask each case N times (latency/determinism)")
    parser.add_argument("--min-interval", type=float, default=8.0, help="seconds between Groq calls (not timed)")
    parser.add_argument("--max-429-rate", type=float, default=0.10)
    parser.add_argument("--tag", default="", help="suffix for the output file name")
    parser.add_argument("--name", default="", help="full output stem (overrides the default name)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--export", type=Path, help="write the deliverable from this result file and exit")
    parser.add_argument("--export-to", type=Path, default=DELIVERABLE)
    parser.add_argument("--quota", action="store_true", help="print Groq's remaining limits and exit")
    parser.add_argument("--rescore", type=Path, help="re-apply the current scorer to a result file's stored answers "
                                                     "(no LLM calls) and write --name")
    args = parser.parse_args()
    if args.export:
        return export_deliverable(args.export, args.export_to)
    if args.quota:
        return quota()
    if args.rescore:
        return rescore(args.rescore, args.name or args.rescore.stem + "_rescored")

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
    records, samples, n429, llm_calls_made, aborted = [], [], 0, 0, False
    outcomes: dict = {}
    started_at = datetime.now(timezone.utc)
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
                        time.sleep(wait)                                     # not part of any latency number
                actual = ask_http(client, app, case, outcomes)
                if actual.get("llm_calls"):
                    llm_calls_made += 1
                if actual.get("llm_cause") == "rate_limited":
                    n429 += 1
                    samples.append(actual)
                    time.sleep(float(actual.get("retry_after") or 10))
                    actual = ask_http(client, app, case, outcomes)          # one runner-level retry
                    llm_calls_made += bool(actual.get("llm_calls"))
                    n429 += actual.get("llm_cause") == "rate_limited"
                if actual.get("llm_calls"):
                    last_llm_call = time.monotonic()
                samples.append(actual)
                scores, ok = check_case(case, actual, texts)
                actual["scores"] = scores
                runs.append((actual, ok))
                if llm_calls_made >= 20 and n429 / llm_calls_made > args.max_429_rate:
                    aborted = True
                    break
            if not runs:
                break
            actual, _ = runs[0]
            ok = all(o for _, o in runs)
            if args.repeat > 1:
                actual["repeats"] = [{"status": a["status"], "pass": o, "include_ok": a["scores"]["include_ok"],
                                      "answer": a.get("answer"), "latency_ms": a["latency_ms"],
                                      "retrieve_ms": a.get("retrieve_ms"), "llm_ms": a.get("llm_ms")} for a, o in runs]
            s = actual["scores"]
            why = "pass" if ok else "FAIL: " + ", ".join(
                k for k, v in [("status", s["status_ok"]), ("must_include", s["include_ok"]),
                               ("must_not_include", s["exclude_ok"]), ("citation", s["citation_ok"]),
                               ("leak", not s["leaks"]), ("unsupported", not s["unsupported"]),
                               ("repeat", all(o for _, o in runs))] if not v)
            record = {**case, "actual": actual, "pass": ok,
                      "notes": f"{case['notes']} | {args.llm}/{args.prompt}/{args.retrieval}/http: {why}"
                               + (f" (reason={actual.get('reason')})" if actual.get("reason") else "")}
            assert tuple(record) == FIELDS
            records.append(record)
            if aborted:
                break
    tmp.cleanup()
    finished_at = datetime.now(timezone.utc)

    summary = {
        "run": {"llm": args.llm, "model": os.environ.get("LLM_MODEL", "openai/gpt-oss-20b") if args.llm == "groq" else "fake",
                "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "300")), "prompt": args.prompt,
                "retrieval": args.retrieval, "split": args.split, "repeat": args.repeat, "path": "http",
                "min_interval_s": args.min_interval if args.llm == "groq" else 0,
                "started_utc": started_at.isoformat(timespec="seconds"),
                "finished_utc": finished_at.isoformat(timespec="seconds"),
                "aborted_429": aborted, "http_429": n429, "llm_calls_made": llm_calls_made},
        "all": summarise(records),
        "dev": summarise([r for r in records if split_of(r) == "dev"]),
        "heldout": summarise([r for r in records if split_of(r) == "heldout"]),
        "per_category": per_category(records),
        "latency": latency_block(samples),
        "determinism": determinism(records),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = args.name or f"{args.llm}_{args.retrieval}_{args.split}" + (f"_{args.tag}" if args.tag else "")
    out = RESULTS_DIR / f"{stem}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    (RESULTS_DIR / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        for r in records:
            a = r["actual"]
            text = (a.get("answer") or a.get("llm_cause") or a.get("detail") or a.get("message") or "")[:84]
            print(f"{r['id']:22} {'PASS' if r['pass'] else 'FAIL'} {a['http_status']} {a['status']:11} "
                  f"{str(a.get('reason') or ''):15} cited={','.join(a['citations']) or '-':6} {a['latency_ms']:6.0f}ms  "
                  + text.replace("\n", " "))
    for key in ("run", "all", "dev", "heldout", "per_category", "latency", "determinism"):
        print(f"{key}: {json.dumps(summary[key], ensure_ascii=False)}")
    print(f"wrote {len(records)} cases to eval/results/{out.name} (+ .summary.json)")
    if args.llm == "fake":
        print("!" * 70)
        print("FakeLLM run: pipeline check only; quality metrics need --llm groq")
        print("!" * 70)
    if aborted:
        print(f"ABORTED: 429 rate {n429}/{llm_calls_made} exceeded {args.max_429_rate:.0%}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
