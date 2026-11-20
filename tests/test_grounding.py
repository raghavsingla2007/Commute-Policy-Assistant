"""P5: citation validation, grounding verifier (with false-positive guards), foreign-company check,
citation rendering from metadata."""

import pytest

from app.grounding import (CitationError, foreign_companies, render_citations, strip_labels, unsupported_values,
                           validate_citations, values_in)
from app.retrieval import RetrievedChunk

ESCORT = ("Section 4 Night Shift Safety\nWomen employees are provided a security escort on drops departing "
          "after 9:00 PM. Drops departing at or before 9:00 PM do not include a security escort.")
ADHOC = ("Section 6 Ad-hoc Trips\nAd-hoc trips must be approved by the employee's reporting manager before "
         "the trip. The cost cap for an ad-hoc trip is ₹1,000 per trip.")
NOSHOW = ("Section 9 No-show Policy\nA no-show is recorded when an employee does not board within 5 minutes. "
          "2 or more no-shows in a calendar month result in a 7-day suspension of company transport.")


# --- citations ----------------------------------------------------------------------------
@pytest.mark.parametrize("labels,available,expected", [
    (["S1"], 3, ["S1"]), (["S2", "S1"], 3, ["S2", "S1"]), (["S1", "S1"], 3, ["S1"]), (["S1", "S2", "S3"], 3, ["S1", "S2", "S3"]),
])
def test_valid_citations(labels, available, expected):
    assert validate_citations("answered", labels, available) == expected


@pytest.mark.parametrize("labels,available,match", [
    ([], 3, "without a citation"),
    (["S4"], 3, "unknown"),
    (["S3"], 2, "unknown"),                 # only 2 excerpts were given
    (["s1"], 3, "unknown"),
    (["Section 4"], 3, "unknown"),
    (["S1", "S2", "S3", "S4"], 3, "max 3"),
])
def test_invalid_citations(labels, available, match):
    with pytest.raises(CitationError, match=match):
        validate_citations("answered", labels, available)


def test_citations_on_not_covered_are_ignored():
    assert validate_citations("not_covered", ["S9"], 3) == []


# --- grounding verifier ---------------------------------------------------------------------
def test_values_are_normalised():
    assert values_in("After 9 PM, ₹1,000 per trip, within five minutes (S1)") == ["21:00", "1000", "5"]


@pytest.mark.parametrize("answer", [
    "Women get a security escort on drops after 9 PM.",
    "Escort is provided after 9:00 pm; at or before 21:00 there is none.",
    "The cap is Rs 1000 per trip.",
    "The cap is ₹1,000 per trip, approved by your reporting manager (S1).",
    "Two or more no-shows in a month lead to a seven-day suspension.",
    "A no-show counts if you don't board within 5 minutes.",
    "Yes. As stated in Section 4, you get an escort.",
])
def test_grounded_answers_pass(answer):
    assert unsupported_values(answer, [ESCORT, ADHOC, NOSHOW]) == []


@pytest.mark.parametrize("answer,bad", [
    ("Women get an escort on drops after 10 PM.", ["22:00"]),             # the old v1 value
    ("The cap is ₹800 per trip.", ["800"]),
    ("The cap is ₹10,000 per trip.", ["10000"]),
    ("Escort after 9:30 PM.", ["21:30"]),
    ("3 no-shows lead to a 30-day suspension.", ["3", "30"]),
])
def test_ungrounded_values_are_caught(answer, bad):
    assert unsupported_values(answer, [ESCORT, ADHOC, NOSHOW]) == bad


def test_values_from_the_question_are_allowed():
    q = "I'm a woman and my drop leaves at 9:30 PM. Will I get an escort?"
    answer = "Yes. Your 9:30 PM drop is after 9:00 PM, so you get a security escort."
    assert unsupported_values(answer, [ESCORT, q]) == []
    assert unsupported_values(answer, [ESCORT]) == ["21:30"]          # without the question it would fail


def test_only_cited_chunks_count_as_sources():
    assert unsupported_values("The cap is ₹1,000 per trip.", [ESCORT]) == ["1000"]


def test_derived_values_are_a_known_false_refusal():
    """Documented limitation (D10): a correct but derived number is rejected."""
    assert unsupported_values("You can miss 1 more cab before suspension.", [NOSHOW]) == ["1"]


# --- foreign companies ------------------------------------------------------------------------
def test_foreign_company_names_detected():
    assert foreign_companies("Unlike Orbitron, Kestrel gives escorts after 9 PM.", "kestrel") == ["orbitron"]
    assert foreign_companies("Tamarind and orbitron rules differ.", "kestrel") == ["orbitron", "tamarind"]
    assert foreign_companies("Kestrel gives escorts after 9 PM.", "kestrel") == []
    assert foreign_companies("Orbitronics is not a company we know.", "kestrel") == []


# --- rendering ------------------------------------------------------------------------------
def chunk(section_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(f"kestrel-transport-policy:v2:s{section_id}:p1", text, "kestrel", "kestrel-transport-policy",
                          2, section_id, text.split("\n")[0].split(" ", 2)[2], 0.7)


def test_citations_rendered_from_metadata():
    long_adhoc = ADHOC + " Any amount above the cap is paid by the employee." * 3
    chunks = [chunk("4", ESCORT), chunk("6", long_adhoc)]
    [c] = render_citations(["S2"], chunks)
    assert (c.doc_id, c.version, c.section_id, c.section_title) == ("kestrel-transport-policy", 2, "6", "Ad-hoc Trips")
    assert c.snippet.startswith("Ad-hoc trips must be approved") and len(c.snippet) <= 201
    assert c.snippet.endswith("…")                                   # long body is cut at a word boundary
    [short] = render_citations(["S1"], chunks)
    assert short.snippet == ESCORT.split("\n", 1)[1]                  # short body is shown whole


def test_question_time_without_pm_supports_answer_with_pm():
    """Found on DEV (k-para-escort, v1+reasoning run): "9:30 at night" in the question, "9:30 PM" in the answer."""
    q = "I'm a woman and my drop leaves at 9:30 at night. Will someone ride along?"
    assert unsupported_values("Yes: 9:30 PM is after 9:00 PM, so you get an escort.", [ESCORT, q]) == []


def test_twelve_hour_fallback_keeps_guards():
    assert unsupported_values("Escort after 21:30.", [ESCORT]) == ["21:30"]              # no 9:30 anywhere
    assert unsupported_values("Escort after 21:00.", ["drops at 19:00 or 09:00"]) == ["21:00"]  # "09:00" is not "9:00"
    assert unsupported_values("Pickup at 9 AM.", ["the 9:00 PM rule"]) == ["09:00"]      # AM never borrows a PM source


def test_strip_labels():
    assert strip_labels("Women get an escort after 9:00 PM. (S3)") == "Women get an escort after 9:00 PM."
    assert strip_labels("Approval needed [S1, S2] before the trip.") == "Approval needed before the trip."
    assert strip_labels("Section S-block parking is not mentioned.") == "Section S-block parking is not mentioned."
