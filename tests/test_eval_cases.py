"""Eval set shape, its link to the ground truth, and the HELD-OUT freeze."""

import json
import re
from pathlib import Path

import pytest

from app.config import KNOWN_COMPANIES
from app.ingest import parse_policy
from eval.freeze import CASES, heldout_sha256, load_cases, split_of
from eval.metrics import contains

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["id", "input", "expected", "actual", "pass", "notes"]
STATUSES = {"answered", "not_covered", "auth_error"}
# During eval Kestrel v2 is active (seed v1 x3, then ingest Kestrel v2).
ACTIVE_FILE = {"kestrel": "kestrel_v2.md", "tamarind": "tamarind_v1.md", "orbitron": "orbitron_v1.md"}

CASES_LIST = load_cases()


def sections_of(company: str) -> dict[str, str]:
    text = (ROOT / "data" / "policies" / ACTIVE_FILE[company]).read_text(encoding="utf-8")
    return {s.section_id: s.text for s in parse_policy(text).sections}


def test_shape_and_counts():
    assert len(CASES_LIST) == 55
    assert len({c["id"] for c in CASES_LIST}) == 55
    for c in CASES_LIST:
        assert list(c) == FIELDS, c["id"]
        assert set(c["input"]) <= {"company", "question", "auth"} and c["input"]["company"] in KNOWN_COMPANIES
        assert c["input"].get("auth", "valid") in {"valid", "malformed", "expired"}
        assert c["actual"] is None and c["pass"] is False
        assert re.match(r"^category=[a-z_]+(,[a-z_]+)*; split=(dev|heldout);", c["notes"]), c["id"]
        assert set(c["expected"]["status"]) <= STATUSES
        assert set(c["expected"]) <= {"status", "sections", "must_include", "must_not_include",
                                      "http_status", "all_sections"}
        if c["expected"].get("all_sections"):
            assert 2 <= len(c["expected"]["sections"]) <= 3, c["id"]     # citations cap is 3
    splits = [split_of(c) for c in CASES_LIST]
    assert (splits.count("dev"), splits.count("heldout")) == (25, 30)


@pytest.mark.parametrize("case", CASES_LIST, ids=lambda c: c["id"])
def test_expected_values_come_from_the_policy_text(case):
    """Each must_include group has an alternative that is literally in an expected section, and no
    must_not_include phrase is in those sections. So expectations trace back to the ground truth.
    Matching uses the scorer's own normalisation (eval.metrics.contains), so "9 PM" == "9:00 PM"."""
    exp = case["expected"]
    if exp["status"] == ["not_covered"] or exp["status"] == ["auth_error"]:
        assert exp["sections"] == [] and exp["must_include"] == []
        return
    secs = sections_of(case["input"]["company"])
    assert exp["sections"] and set(exp["sections"]) <= set(secs), case["id"]
    text = " ".join(secs[s] for s in exp["sections"])
    for group in exp["must_include"]:
        assert any(contains(text, alt) for alt in group), (case["id"], group)
    for phrase in exp["must_not_include"]:
        assert not contains(text, phrase), (case["id"], phrase)


def frozen_hashes() -> list[str]:
    log = (ROOT / "PROJECT_LOG.md").read_text(encoding="utf-8")
    return re.findall(r"HELDOUT sha256=([0-9a-f]{64})", log)


def test_heldout_matches_last_logged_freeze():
    hashes = frozen_hashes()
    assert hashes, "no HELDOUT freeze recorded in PROJECT_LOG.md §8.5"
    assert heldout_sha256() == hashes[-1], (
        "HELD-OUT cases changed. Revert, or log an unfreeze decision with the new hash in §8.5 "
        f"(python -m eval.freeze -> {heldout_sha256()})")


def test_freeze_detects_heldout_edits_but_not_dev_edits(tmp_path):
    cases = load_cases()
    copy = tmp_path / "cases.jsonl"

    def write(cs):
        copy.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cs), encoding="utf-8")
        return heldout_sha256(copy)

    original = write(cases)
    assert original == heldout_sha256(CASES)
    dev = next(i for i, c in enumerate(cases) if split_of(c) == "dev")
    held = next(i for i, c in enumerate(cases) if split_of(c) == "heldout")

    edited = json.loads(json.dumps(cases))
    edited[dev]["input"]["question"] += " (tuned)"
    assert write(edited) == original                       # DEV is free to change

    edited = json.loads(json.dumps(cases))
    edited[held]["expected"]["must_not_include"].append("x")
    assert write(edited) != original                       # any HELD-OUT edit is caught

    edited = json.loads(json.dumps(cases))
    edited[held]["notes"] = edited[held]["notes"].replace("split=heldout", "split=dev")
    assert write(edited) != original                       # moving a case out of HELD-OUT is caught
