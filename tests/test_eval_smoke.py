"""The eval runner works end to end over HTTP with FakeLLM: exit 0, exact JSONL fields, auth case = 401,
Kestrel v2 ingested through the admin API before any question is asked."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["id", "input", "expected", "actual", "pass", "notes"]


def test_runner_dev_split_with_fake_llm():
    env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
    proc = subprocess.run([sys.executable, "-m", "eval.run_eval", "--split", "all", "--quiet", "--tag", "smoke"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "FakeLLM run: pipeline check only; quality metrics need --llm groq" in proc.stdout
    assert "upgraded via POST /admin/policies" in proc.stdout and "'version': 2" in proc.stdout
    out = ROOT / "eval" / "results" / "fake_vector_all_smoke.jsonl"
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 55 and all(list(r) == FIELDS for r in records)
    assert all(r["notes"].startswith("category=") for r in records)
    auth = next(r for r in records if r["id"] == "x-auth-malformed")
    assert auth["actual"]["http_status"] == 401 and auth["pass"] is True
    summary = json.loads((ROOT / "eval" / "results" / "fake_vector_all_smoke.summary.json").read_text())
    assert summary["all"]["leaks"] == 0 and summary["all"]["unsupported_claims"] == 0
    assert summary["all"]["llm_calls_max"] == 1 and summary["all"]["auth"] == "1/1"
    assert summary["dev"]["cases"] + summary["heldout"]["cases"] == 55
    assert summary["latency"]["samples_200"] == 54 and summary["determinism"]["cases_with_repeats"] == 0
    assert {r["actual"]["policy_version"] for r in records if r["input"]["company"] == "kestrel"
            and r["actual"]["http_status"] == 200} == {2}
    out.unlink()
    (ROOT / "eval" / "results" / "fake_vector_all_smoke.summary.json").unlink()


def test_export_writes_exactly_six_fields(tmp_path):
    src = tmp_path / "run.jsonl"
    src.write_text(json.dumps({"id": "a", "input": {}, "expected": {}, "actual": {"status": "answered"}, "pass": True,
                               "notes": "category=direct; split=dev;"}) + "\n", encoding="utf-8")
    target = tmp_path / "eval_set.jsonl"
    proc = subprocess.run([sys.executable, "-m", "eval.run_eval", "--export", str(src), "--export-to", str(target)],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    [record] = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert list(record) == FIELDS and record["pass"] is True
