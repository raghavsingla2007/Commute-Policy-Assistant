"""Evaluation runner (P0 stub): writes one valid JSONL line with the six required fields.

Exit code contract: 0 whenever the run finishes (even if quality cases fail), non-zero only on a crash.
"""

import argparse
import json
import os
import secrets
from pathlib import Path

from app.config import load_settings

FIELDS = ("id", "input", "expected", "actual", "pass", "notes")
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation set")
    parser.add_argument("--llm", choices=["fake", "groq"], default="fake")
    parser.add_argument("--retrieval", choices=["vector", "hybrid"], default="vector")
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="all")
    args = parser.parse_args()

    # The eval is a closed system: it mints its own tokens, so it uses a fresh random secret per run
    # and never depends on the server's JWT_SECRET or DEV_MODE.
    os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
    load_settings().validate()
    record = {
        "id": "p0-stub",
        "input": {"company": "kestrel", "question": "stub"},
        "expected": {"status": "answered"},
        "actual": None,
        "pass": False,
        "notes": "category=stub; split=dev; P0 stub, pipeline not built yet",
    }
    assert tuple(record) == FIELDS

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{args.llm}_{args.retrieval}_{args.split}.jsonl"
    out.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote 1 case to {out}")
    if args.llm == "fake":
        print("!" * 70)
        print("FakeLLM run: pipeline check only; quality metrics need --llm groq")
        print("!" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
