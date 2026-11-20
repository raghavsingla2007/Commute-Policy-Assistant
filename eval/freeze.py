"""HELD-OUT freeze: a sha256 over every held-out case, so any edit to them is detectable.

    python -m eval.freeze        # prints the current hash

The frozen hash lives in PROJECT_LOG.md §8.5 as a line containing `HELDOUT sha256=<hex>`.
tests/test_eval_cases.py fails unless the current hash equals the LAST such line. Changing a held-out
case therefore requires a logged "unfreeze" decision: a new §8.5 row with the new hash and the reason.
"""

import hashlib
import json
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases.jsonl"


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_of(case: dict) -> str:
    return case["notes"].split(";")[1].strip().removeprefix("split=")


def heldout_sha256(path: Path = CASES) -> str:
    """Canonical JSON of each held-out case (sorted keys), sorted by id, one per line."""
    held = sorted((c for c in load_cases(path) if split_of(c) == "heldout"), key=lambda c: c["id"])
    canon = "\n".join(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in held)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    print(heldout_sha256())
