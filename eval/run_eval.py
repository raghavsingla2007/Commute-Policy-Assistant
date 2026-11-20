"""Evaluation runner.

    python -m eval.run_eval [--split dev|heldout|all] [--retrieval vector|hybrid] [--llm fake|groq]

P4 state: the answer pipeline (/ask) doesn't exist yet, so the runner measures RETRIEVAL ONLY:
hit@1, hit@3 and MRR against expected sections (answerable cases), top-1 similarity and the top-1/top-2
margin (all cases), and retrieval latency. Answer scoring arrives in P7.

It builds its own fresh store in a temp dir (seed v1 x3, then Kestrel v2), so results never depend on
the server's data. Exit code: 0 whenever the run finishes (even if cases fail), non-zero only on a crash.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import secrets
import statistics
import tempfile
from pathlib import Path

from app.auth import Principal
from app.config import PROJECT_ROOT, load_settings
from app.embeddings import shared_embedder
from app.ingest import ingest_policy, seed_if_empty
from app.retrieval import passes_prefilter, retrieve
from app.store import Store
from eval.freeze import CASES, load_cases, split_of
from eval.metrics import percentile, retrieval_ranks

FIELDS = ("id", "input", "expected", "actual", "pass", "notes")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
POLICIES = PROJECT_ROOT / "data" / "policies"


def kind_of(case: dict) -> str:
    status = case["expected"]["status"]
    if case["input"].get("auth", "valid") != "valid":
        return "auth"
    if status == ["answered"]:
        return "answerable"
    if status == ["not_covered"]:
        return "not_covered"
    return "either"        # answered or not_covered both acceptable (traps, injections)


def build_store(data_dir: Path) -> Store:
    store = Store(data_dir, shared_embedder())
    seed_if_empty(store, POLICIES)
    ingest_policy(store, "kestrel", (POLICIES / "kestrel_v2.md").read_text(encoding="utf-8"))
    store.warm()
    return store


async def run_case(store: Store, settings, case: dict) -> tuple[dict, bool, str]:
    kind = kind_of(case)
    if kind == "auth":
        return {"skipped": "auth case: needs /ask (P6)"}, False, "retrieval-only: auth case not run"
    principal = Principal(case["input"]["company"], "employee", f"eval-{case['id']}")
    result = await retrieve(store, principal, case["input"]["question"], settings)
    sections = [c.section_id for c in result.chunks]
    sims = [round(c.similarity, 4) for c in result.chunks]
    expected = case["expected"]["sections"]
    rank = retrieval_ranks(sections, expected, case["expected"].get("all_sections", False))
    actual = {
        "mode": "retrieval_only",
        "company_id": result.company_id, "policy_version": result.version,
        "retrieved": [{"section": s, "similarity": sim} for s, sim in zip(sections, sims)],
        "top1_similarity": sims[0] if sims else None,
        "margin_top1_top2": round(sims[0] - sims[1], 4) if len(sims) > 1 else None,
        "rank": rank,
        "prefilter": "pass" if passes_prefilter(result, settings.not_covered_min_sim) else "low_relevance",
        "retrieve_ms": round(result.retrieve_ms, 1),
        "foreign_chunks": sum(c.company_id != principal.company_id for c in result.chunks),
    }
    if kind == "answerable":
        ok = rank is not None and rank <= 3 and actual["prefilter"] == "pass"
        why = "retrieval-only: pass = expected section(s) in top 3 and not pre-filtered"
    else:
        ok = actual["foreign_chunks"] == 0
        why = "retrieval-only: pass = no foreign chunk (not-covered verdict needs the LLM, P5+)"
    return actual, ok, why


def summarise(records: list[dict], settings) -> dict:
    def kind(r):
        return kind_of(r)

    answerable = [r for r in records if kind(r) == "answerable"]
    ranks = [r["actual"]["rank"] for r in answerable]
    run = [r for r in records if "retrieve_ms" in r["actual"]]
    lat = [r["actual"]["retrieve_ms"] for r in run]
    nc = [r for r in records if kind(r) == "not_covered"]
    return {
        "cases": len(records), "answerable": len(answerable), "not_covered": len(nc),
        "hit@1": round(sum(1 for x in ranks if x == 1) / len(ranks), 3) if ranks else None,
        "hit@3": round(sum(1 for x in ranks if x is not None and x <= 3) / len(ranks), 3) if ranks else None,
        "mrr": round(sum(1 / x for x in ranks if x) / len(ranks), 3) if ranks else None,
        "answerable_prefiltered": sum(r["actual"]["prefilter"] == "low_relevance" for r in answerable),
        "not_covered_prefiltered": sum(r["actual"]["prefilter"] == "low_relevance" for r in nc),
        "threshold": settings.not_covered_min_sim,
        "foreign_chunks": sum(r["actual"]["foreign_chunks"] for r in run),
        "retrieve_ms": {"first": lat[0] if lat else None,
                        "p50": round(percentile(lat, 50), 1) if lat else None,
                        "p95": round(percentile(lat, 95), 1) if lat else None,
                        "max": max(lat) if lat else None, "n": len(lat)},
        "top1_answerable": sorted(r["actual"]["top1_similarity"] for r in answerable),
        "top1_not_covered": sorted(r["actual"]["top1_similarity"] for r in nc),
    }


def print_table(records: list[dict]) -> None:
    print(f"{'id':22} {'kind':12} {'exp':7} {'got (sec:sim)':38} {'rank':4} {'margin':6} {'pre':4} pass")
    for r in records:
        a = r["actual"]
        if "retrieved" not in a:
            print(f"{r['id']:22} {kind_of(r):12} {'-':7} {a.get('skipped', '')}")
            continue
        got = " ".join(f"{x['section']}:{x['similarity']:.3f}" for x in a["retrieved"])
        exp = ",".join(r["expected"]["sections"]) or "-"
        print(f"{r['id']:22} {kind_of(r):12} {exp:7} {got:38} {str(a['rank'] or '-'):4} "
              f"{a['margin_top1_top2'] or 0:6.3f} {'ok' if a['prefilter'] == 'pass' else 'LOW':4} {r['pass']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set (P4: retrieval only)")
    parser.add_argument("--llm", choices=["fake", "groq"], default="fake")
    parser.add_argument("--retrieval", choices=["vector", "hybrid"], default="vector")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    parser.add_argument("--quiet", action="store_true", help="don't print the per-case table")
    args = parser.parse_args()

    # The eval is a closed system: it mints its own tokens, so it uses a fresh random secret per run
    # and never depends on the server's JWT_SECRET or DEV_MODE.
    os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
    settings = dataclasses.replace(load_settings(), retrieval_mode=args.retrieval)
    settings.validate()

    cases = [c for c in load_cases(CASES) if args.split == "all" or split_of(c) == args.split]
    records = []
    with tempfile.TemporaryDirectory() as tmp:
        store = build_store(Path(tmp) / "data")
        try:
            for case in cases:
                actual, ok, why = asyncio.run(run_case(store, settings, case))
                record = {**case, "actual": actual, "pass": ok, "notes": f"{case['notes']} | {why}"}
                assert tuple(record) == FIELDS
                records.append(record)
        finally:
            store.close()

    summary = summarise(records, settings)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"retrieval-only_{args.retrieval}_{args.split}"
    out = RESULTS_DIR / f"{stem}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    (RESULTS_DIR / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.quiet:
        print_table(records)
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("top1_")}))
    print(f"wrote {len(records)} cases to eval/results/{out.name} (+ .summary.json)")
    print(f"answerable top-1 sims: {summary['top1_answerable']}")
    print(f"not-covered top-1 sims: {summary['top1_not_covered']}")
    print("!" * 70)
    print("FakeLLM run: pipeline check only; quality metrics need --llm groq"
          if args.llm == "fake" else "Retrieval-only run: --llm has no effect until P7")
    print("!" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
