"""Eval scoring: phrase matching (via app.textnorm, the same rules the grounding verifier uses),
retrieval ranks, percentiles, and per-case answer scoring."""

import statistics

from app.textnorm import contains, normalise  # noqa: F401  (re-exported for tests and the runner)

# ---------------------------------------------------------------- retrieval metrics

def retrieval_ranks(retrieved_sections: list[str], expected: list[str], require_all: bool = False) -> int | None:
    """1-based rank at which the expectation is met, or None if never met within the list.

    Default: rank of the first expected section. require_all (multi-section cases): rank at which
    the LAST of the expected sections has appeared."""
    if not expected:
        return None
    positions = {s: retrieved_sections.index(s) + 1 for s in expected if s in retrieved_sections}
    if require_all:
        return max(positions.values()) if len(positions) == len(set(expected)) else None
    return min(positions.values()) if positions else None


def percentile(values: list[float], pct: int) -> float:
    """pct in 1..99, via statistics.quantiles(n=100, method='inclusive')."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


# ---------------------------------------------------------------- answer scoring (P5)

def score_answer(case: dict, actual: dict) -> dict:
    """Rule-based scoring of one /ask result against the case's expectations (D17, D46).

    status_ok:   actual status is one of the allowed statuses
    include_ok:  when answered: every must_include group has an alternative in the answer
    exclude_ok:  no must_not_include phrase in the user-visible text
    citation_ok: when answered and sections are expected: cited sections hit them (all of them for
                 multi-section cases)
    """
    exp = case["expected"]
    status = actual["status"]
    shown = actual.get("answer") or actual.get("message") or ""
    status_ok = status in exp["status"]
    include_ok = exclude_ok = citation_ok = True
    if status == "answered":
        include_ok = all(any(contains(shown, alt) for alt in group) for group in exp["must_include"])
        if exp["sections"]:
            cited = set(actual.get("cited_sections", []))
            need = set(exp["sections"])
            citation_ok = need <= cited if exp.get("all_sections") else bool(need & cited)
    exclude_ok = not any(contains(shown, phrase) for phrase in exp["must_not_include"])
    return {"status_ok": status_ok, "include_ok": include_ok, "exclude_ok": exclude_ok,
            "citation_ok": citation_ok, "pass": status_ok and include_ok and exclude_ok and citation_ok}
