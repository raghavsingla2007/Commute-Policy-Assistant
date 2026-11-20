"""Recompute every number the README and docs/EVAL_REPORT.md quote, from the committed result files and the
test suite, and check that each document states it.

    python -m scripts.check_docs_numbers               # also counts tests with `pytest --collect-only`
    python -m scripts.check_docs_numbers --skip-pytest

How it works:
1. FACTS are recomputed from the raw JSONL records (pass counts, retrieval ranks, latency percentiles), then
   cross-checked against the run's own .summary.json. A disagreement there is a data problem and is reported.
2. CLAIMS are exact phrases built from the facts. Each must appear in the named document. A missing phrase
   means the document is stale or wrong: fix the document, never the data.
3. A few numbers can't be recomputed from committed files (Groq's own limits, the P8 latency A/B and image
   size). They are listed at the end as "not recomputed", with their source.

Exit code 0 = every check passed; 1 = at least one mismatch (each is printed).
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
README = ROOT / "README.md"
REPORT = ROOT / "docs" / "EVAL_REPORT.md"
DESIGN = ROOT / "docs" / "DESIGN_DECISIONS.md"
LOG = ROOT / "PROJECT_LOG.md"            # private, gitignored: checked only when present
FIELDS = ["id", "input", "expected", "actual", "pass", "notes"]

problems: list[str] = []


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summary(stem: str) -> dict:
    return json.loads((RESULTS / f"{stem}.summary.json").read_text(encoding="utf-8"))


def split(rec: dict) -> str:
    return rec["notes"].split(";")[1].strip().removeprefix("split=")


def frac(xs) -> str:
    xs = list(xs)
    return f"{sum(xs)}/{len(xs)}"


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20)[18]          # the same definition the eval runner uses


def same(label: str, recomputed, stored) -> None:
    if recomputed != stored:
        problems.append(f"DATA  {label}: recomputed {recomputed!r} but the summary file says {stored!r}")


# ---------------------------------------------------------------- facts from result files

def run_facts(stem: str) -> dict:
    """Pass and retrieval metrics per split, recomputed from one result file's records."""
    recs = jsonl(RESULTS / f"{stem}.jsonl")
    out = {}
    for name, sel in (("dev", lambda r: split(r) == "dev"), ("heldout", lambda r: split(r) == "heldout"),
                      ("all", lambda r: True)):
        rs = [r for r in recs if sel(r)]
        answerable = [r for r in rs if r["expected"]["status"] == ["answered"] and r["input"].get("auth", "valid") == "valid"]
        ranks = [r["actual"]["scores"]["retrieval_rank"] for r in answerable]
        f = {
            "pass": frac(r["pass"] for r in rs),
            "hit@1": frac(x == 1 for x in ranks),
            "hit@3": frac(x is not None and x <= 3 for x in ranks),
            "mrr": round(sum(1 / x for x in ranks if x) / len(ranks), 3),
            "leaks": sum(bool(r["actual"]["scores"]["leaks"]) for r in rs),
            "unsupported_claims": sum(bool(r["actual"]["scores"]["unsupported"]) for r in rs),
            "llm_calls_max": max(r["actual"].get("llm_calls", 0) for r in rs),
        }
        stored = summary(stem)[name]
        for key, value in f.items():
            same(f"{stem} {name} {key}", value, stored[key])
        out[name] = {**stored, **f}
    out["ranks"] = {r["id"]: r["actual"]["scores"]["retrieval_rank"] for r in recs
                    if r["expected"]["status"] == ["answered"] and r["input"].get("auth", "valid") == "valid"}
    out["records"] = {r["id"]: r for r in recs}
    out["run"] = summary(stem)["run"]
    out["per_category"] = summary(stem)["per_category"]
    out["latency"] = summary(stem)["latency"]
    return out


def latency_facts(stem: str) -> dict:
    """Percentiles over every repeat that returned 200 (answered or not_covered)."""
    samples = [x for r in jsonl(RESULTS / f"{stem}.jsonl") for x in r["actual"]["repeats"]]
    ok = [x for x in samples if x["status"] in ("answered", "not_covered")]
    wall = [x["latency_ms"] for x in ok]
    llm = [x["llm_ms"] for x in ok if x.get("llm_ms")]
    retr = [x["retrieve_ms"] for x in ok]
    f = {"n": len(wall), "p50": round(statistics.median(wall)), "p95": round(p95(wall)), "max": round(max(wall)),
         "retr_p50": round(statistics.median(retr)), "retr_p95": round(p95(retr)),
         "llm_p50": round(statistics.median(llm)), "llm_p95": round(p95(llm)), "llm_n": len(llm),
         "requests": len(samples), "unavailable": sum(x["status"] == "unavailable" for x in samples)}
    s = summary(stem)
    same(f"{stem} wall p95", round(p95(wall), 1), s["latency"]["wall_ms"]["p95"])
    same(f"{stem} samples_200", f["n"], s["latency"]["samples_200"])
    f["http_429"] = s["run"]["http_429"]
    f["llm_calls_made"] = s["run"]["llm_calls_made"]
    f["status_or_key_fact_flips"] = s["determinism"]["status_or_key_fact_flips"]
    f["wording"] = s["determinism"]["cases_with_different_wording"]
    f["key_fact_flips"] = sum(len({x["include_ok"] for x in r["actual"]["repeats"]}) > 1
                              for r in jsonl(RESULTS / f"{stem}.jsonl"))
    return f


def deliverable_facts() -> dict:
    lines = (ROOT / "eval" / "eval_set.jsonl").read_text(encoding="utf-8").splitlines()
    recs = [json.loads(line) for line in lines]
    if any(list(r) != FIELDS for r in recs):
        problems.append("DATA  eval/eval_set.jsonl: a record does not have exactly the six fields in order")
    cases = jsonl(ROOT / "eval" / "cases.jsonl")
    if [r["id"] for r in recs] != [c["id"] for c in cases]:
        problems.append("DATA  eval/eval_set.jsonl ids differ from eval/cases.jsonl")
    return {"lines": len(lines), "pass": frac(r["pass"] is True for r in recs)}


def case_facts() -> dict:
    cases = jsonl(ROOT / "eval" / "cases.jsonl")
    dev = [c for c in cases if split(c) == "dev"]
    held = [c for c in cases if split(c) == "heldout"]
    answerable = lambda cs: sum(c["expected"]["status"] == ["answered"] for c in cs)  # noqa: E731
    by_category: dict[str, list[int]] = {}
    for c in cases:
        for cat in c["notes"].split(";")[0].removeprefix("category=").split(","):
            by_category.setdefault(cat, [0, 0])[0 if split(c) == "dev" else 1] += 1
    return {"total": len(cases), "dev": len(dev), "heldout": len(held), "by_category": by_category,
            "dev_answerable": answerable(dev), "heldout_answerable": answerable(held)}


def freeze_facts() -> dict:
    from eval.freeze import heldout_sha256
    logged = re.findall(r"HELDOUT sha256=([0-9a-f]{64})", (ROOT / "eval" / "FREEZE.md").read_text(encoding="utf-8"))
    if heldout_sha256() != logged[-1]:
        problems.append("DATA  HELD-OUT hash differs from the last line of eval/FREEZE.md")
    return {"hash": logged[-1]}


def config_facts() -> dict:
    """Defaults as the code sets them (environment overrides ignored on purpose)."""
    import os
    saved = dict(os.environ)
    os.environ.clear()
    try:
        from app.config import load_settings
        s = load_settings()
    finally:
        os.environ.update(saved)
    from app.auth import LEEWAY_S
    from app.config import MIN_JWT_SECRET_LENGTH
    from app.middleware import ADMIN_BODY_LIMIT, DEFAULT_BODY_LIMIT
    return {"min_sim": s.not_covered_min_sim, "k": s.retrieval_k, "retrieval_timeout": s.retrieval_timeout_s,
            "llm_timeout": s.llm_timeout_s, "deadline": s.request_timeout_s, "max_chars": s.max_question_chars,
            "ttl": s.jwt_ttl_s, "max_tokens": s.llm_max_tokens, "model": s.llm_model,
            "ask_kib": DEFAULT_BODY_LIMIT // 1024, "admin_kib": ADMIN_BODY_LIMIT // 1024,
            "jwt_min": MIN_JWT_SECRET_LENGTH, "leeway": LEEWAY_S}


def hybrid_code_lines() -> int:
    """Non-comment, non-blank lines of the BM25 + fusion code (tokenize .. RETRIEVERS) in app/retrieval.py."""
    text = (ROOT / "app" / "retrieval.py").read_text(encoding="utf-8")
    block = text[text.index("_WORD = re.compile"):text.index("RETRIEVERS: dict")]
    return sum(1 for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#"))


def test_facts() -> dict:
    """Collected test count and the number of bad-token cases, from `pytest --collect-only -q`."""
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    m = re.search(r"(\d+) tests? collected", out)
    return {"tests": int(m.group(1)) if m else None,
            "bad_token_cases": len(re.findall(r"test_auth.py::test_bad_token_rejected\[", out))}


# ---------------------------------------------------------------- claims: phrases each document must contain

def claims(F: dict) -> list[tuple[Path, str]]:
    v, h, raw, lat, cases, cfg = F["vector"], F["hybrid"], F["raw"], F["latency"], F["cases"], F["config"]
    vd, vh, va = v["dev"], v["heldout"], v["all"]
    hd, hh, ha = h["dev"], h["heldout"], h["all"]
    common = [
        f"DEV {cases['dev']}, HELD-OUT {cases['heldout']}",
        f"HELD-OUT {vh['pass']}",
        f"DEV {vd['pass']}",
        f"raw {raw['heldout']['pass']}",
        f"p95 {lat['p95']:,} ms",
        f"{va['hit@1']} · {va['hit@3']} · {va['mrr']:.3f}",
        f"{ha['hit@1']} · {ha['hit@3']} · {ha['mrr']:.3f}",
        f"{vd['mrr']:.3f} → {hd['mrr']:.3f}",
        f"{vh['mrr']:.3f} → {hh['mrr']:.3f}",
        f"hybrid DEV {hd['pass']}",
    ]
    readme = common + [
        f"{cases['total']} cases",
        f"p50 {lat['p50']:,} ms",
        f"max {lat['max']:,} ms",
        f"{lat['n']} successful responses",
        f"not-covered {va['not_covered_accuracy']}",
        f"citations correct {va['citation_correct']}",
        f"false refusals {vh['false_refusals']}",
        f"leaks {va['leaks']}",
        f"unsupported claims {va['unsupported_claims']}",
        f"LLM calls per question, max {va['llm_calls_max']}",
        f"| `NOT_COVERED_MIN_SIM` | {cfg['min_sim']} |",
        f"| `RETRIEVAL_K` | {cfg['k']} |",
        f"| `LLM_TIMEOUT_S` / `RETRIEVAL_TIMEOUT_S` / `REQUEST_TIMEOUT_S` | {cfg['llm_timeout']} / "
        f"{cfg['retrieval_timeout']} / {cfg['deadline']} |",
        f"| `LLM_MODEL` | `{cfg['model']}` |",
        f"{cfg['jwt_min']} characters",
        f"{cfg['leeway']} s leeway",
        f"({F['bad_token_cases']} cases)" if F.get("bad_token_cases") else "",
        f"{F['held_fail_refused']} refusals",
        f"{F['held_fail_answered']} correct answers the phrase lists don't match",
        f"{cfg['retrieval_timeout']} s retrieval",
        f"{cfg['llm_timeout']} s LLM",
        f"{cfg['deadline']} s overall",
        f"{cfg['max_chars']} characters",
        f"{cfg['ask_kib']} KiB",
        f"{cfg['admin_kib']} KiB",
        f"+{F['hybrid_lines']} lines",
        f"multi-section {v['per_category']['multi_section']}",
    ]
    report = common + [
        f"| Pass | **{vd['pass']}** | **{vh['pass']}** (raw {raw['heldout']['pass']}) | {va['pass']} (raw {raw['all']['pass']}) |",
        f"| Status accuracy | {vd['status_accuracy']} | {vh['status_accuracy']} | {va['status_accuracy']} |",
        f"| Not-covered accuracy | {vd['not_covered_accuracy']} | {vh['not_covered_accuracy']} | {va['not_covered_accuracy']} |",
        f"| Citation correct (answered) | {vd['citation_correct']} | {vh['citation_correct']} | {va['citation_correct']} |",
        f"| Client wall clock (200 responses) | {lat['p50']} ms | **{lat['p95']:,} ms** | {lat['max']:,} ms | {lat['n']} |",
        f"| Retrieval (`retrieve_ms`) | {lat['retr_p50']} ms | {lat['retr_p95']} ms | — | {lat['n']} |",
        f"| LLM call (`llm_ms`) | {lat['llm_p50']} ms | {lat['llm_p95']:,} ms | — | {lat['llm_n']} |",
        f"{lat['requests']} requests",
        f"{lat['llm_calls_made']} LLM calls",
        f"Key facts changed in **{lat['key_fact_flips']}** cases",
        f"wording differed in {lat['wording']} cases",
        f"+{F['hybrid_lines']} lines",
        f"`{F['freeze']['hash']}`",
        f"{F['deliverable']['lines']} lines",
        f"{F['deliverable']['pass']}",
    ]
    report += [
        f"| Retrieval hit@1 · hit@3 · MRR | {vd['hit@1']} · {vd['hit@3']} · {vd['mrr']:.3f} | "
        f"{vh['hit@1']} · {vh['hit@3']} · {vh['mrr']:.3f} | {va['hit@1']} · {va['hit@3']} · {va['mrr']:.3f} |",
        f"| False refusals (answerable → not covered) | {vd['false_refusals']} | {vh['false_refusals']} | {va['false_refusals']} |",
        f"Status changed in {lat['status_or_key_fact_flips']} cases",
        f"{v['latency']['wall_ms']['p95']:,.0f} ms → {h['latency']['wall_ms']['p95']:,.0f} ms",
        f"{v['latency']['retrieve_ms']['p95']} ms → {h['latency']['retrieve_ms']['p95']} ms",
    ]
    report += [f"{cat} {res}" for cat, res in v["per_category"].items() if res.split("/")[0] != res.split("/")[1]
               and cat != "multi_section"] + [f"multi_section **{v['per_category']['multi_section']}**"]
    report += [f"| {cat} | {d} | {ho} |" for cat, (d, ho) in cases["by_category"].items()]
    report += [f"| {i} |" for i in F["held_fail_ids"]]
    for case_id, (a, b) in F["rank_changes"].items():
        report.append(f"| {case_id} | {a if a else 'not retrieved'} | {b if b else '**not retrieved**'} |")
    design = [f"p95 {lat['p95']:,} ms", f"{vd['mrr']:.3f} → {hd['mrr']:.3f}", f"({cfg['min_sim']})",
              f"{cfg['retrieval_timeout']} s retrieval, {cfg['llm_timeout']} s LLM, {cfg['deadline']} s overall",
              f"{cfg['leeway']} s leeway", f"{cfg['max_tokens']} completion tokens"]
    out = [(README, c) for c in readme] + [(REPORT, c) for c in report] + [(DESIGN, c) for c in design]
    if F.get("tests") is not None:
        out.append((README, f"{F['tests']} tests"))
    if F.get("fake") is not None:
        out.append((README, f"FakeLLM run passes {F['fake']}"))
    if LOG.exists():                                       # private log: the headline numbers must agree too
        out += [(LOG, c) for c in (f"| Pass | {vd['pass']} | {vh['pass']} (raw {raw['heldout']['pass']}) |",
                                    f"**p95 {lat['p95']} ms**", f"{ha['hit@1']} · {ha['hit@3']} · {ha['mrr']:.3f}",
                                    f"+{F['hybrid_lines']} code lines")]
    return out


NOT_RECOMPUTED = [
    ("Groq free tier: 1,000 requests/day, 8,000 tokens/minute", "Groq response headers x-ratelimit-limit-*, checked 2026-11-20 (`python -m eval.run_eval --quota`)"),
    ("Hybrid latency A/B: +2.7 ms retrieval p95", "P8 in-process A/B measurement (not a committed result file)"),
    ("Image size +18.5 KB with rank-bm25; BM25 warm-up 5-11 ms; separate-run +51.7 ms", "`docker image inspect` and startup logs, P8"),
    ("DEV similarity overlap 0.235-0.516, lowest DEV answerable 0.222 (DESIGN_DECISIONS D9/D52)",
     "P4 retrieval-only DEV run (retrieval-only_vector_dev.jsonl, not committed); rerun: python -m eval.run_eval --split dev with P4 code"),
    ("Query embedding ~490 ms -> ~26 ms, max vector difference ~1e-7 (D31)", "P2 timing; the equality is asserted by tests/test_embeddings.py"),
    ("FakeLLM numbers", "regenerated by the gate's `python -m eval.run_eval` (fake results are not committed)"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-pytest", action="store_true")
    args = ap.parse_args()

    vector = run_facts("groq_vector_full_p7_rescored")
    hybrid = run_facts("groq_hybrid_full_p8")
    raw = run_facts("groq_vector_full_p7")
    F = {"vector": vector, "hybrid": hybrid, "raw": raw, "latency": latency_facts("groq_vector_full_p7_latency"),
         "cases": case_facts(), "config": config_facts(), "freeze": freeze_facts(),
         "deliverable": deliverable_facts(), "hybrid_lines": hybrid_code_lines()}
    F["rank_changes"] = {i: (vector["ranks"][i], hybrid["ranks"][i]) for i in vector["ranks"]
                         if vector["ranks"][i] != hybrid["ranks"][i]}
    fake = RESULTS / "fake_vector_all.summary.json"
    F["fake"] = json.loads(fake.read_text())["all"]["pass"] if fake.exists() else None
    F.update({"tests": None, "bad_token_cases": None} if args.skip_pytest else test_facts())
    held_fail = [r for r in vector["records"].values() if split(r) == "heldout" and not r["pass"]]
    F["held_fail_refused"] = sum(r["actual"]["status"] == "not_covered" for r in held_fail)
    F["held_fail_answered"] = sum(r["actual"]["status"] == "answered" for r in held_fail)
    F["held_fail_ids"] = [r["id"] for r in held_fail]

    if F["deliverable"]["pass"] != vector["all"]["pass"]:
        problems.append("DATA  eval/eval_set.jsonl pass count differs from the vector run it was exported from")

    docs = {p: p.read_text(encoding="utf-8") for p in (README, REPORT, DESIGN, LOG) if p.exists()}
    checked = 0
    for path, phrase in claims(F):
        if not phrase:
            continue
        checked += 1
        if path not in docs:
            problems.append(f"DOC   {path.name} is missing")
        elif phrase not in docs[path]:
            problems.append(f"DOC   {path.name}: expected to state {phrase!r}")

    va, lat = vector["all"], F["latency"]
    print("Recomputed from eval/results/*.jsonl (cross-checked with *.summary.json):")
    print(f"  cases {F['cases']}")
    print(f"  vector (P7 rescored): DEV {vector['dev']['pass']}, HELD-OUT {vector['heldout']['pass']} "
          f"(raw {raw['heldout']['pass']}), all {va['pass']}; hit@1 {va['hit@1']} hit@3 {va['hit@3']} MRR {va['mrr']}; "
          f"leaks {va['leaks']}, unsupported {va['unsupported_claims']}")
    print(f"  hybrid (P8): DEV {hybrid['dev']['pass']}, HELD-OUT {hybrid['heldout']['pass']}; "
          f"MRR DEV {vector['dev']['mrr']}→{hybrid['dev']['mrr']}, HELD-OUT {vector['heldout']['mrr']}→"
          f"{hybrid['heldout']['mrr']}, all {va['mrr']}→{hybrid['all']['mrr']}")
    print(f"  rank changes vector→hybrid: {F['rank_changes']}")
    print(f"  latency (repeat 3): p50 {lat['p50']} p95 {lat['p95']} max {lat['max']} ms over {lat['n']}; "
          f"429s {lat['http_429']}, 503s {lat['unavailable']}")
    print(f"  deliverable {F['deliverable']}; freeze {F['freeze']['hash'][:12]}…; hybrid code lines {F['hybrid_lines']}")
    print(f"  config {F['config']}")
    print(f"  tests collected: {F['tests'] if F['tests'] is not None else 'not counted (--skip-pytest)'}; "
          f"FakeLLM pass: {F['fake'] or 'no fake_vector_all.summary.json (run the gate eval first)'}")
    print(f"Checked {checked} phrases in {', '.join(p.name for p in docs)}.")
    print("Not recomputed (no committed source file):")
    for what, source in NOT_RECOMPUTED:
        print(f"  - {what}: {source}")
    if problems:
        print(f"\n{len(problems)} MISMATCH(ES):")
        for p in problems:
            print("  " + p)
        return 1
    print("\nAll numbers match.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
