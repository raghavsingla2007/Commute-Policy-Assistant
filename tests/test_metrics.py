"""Eval scorer normalisation and retrieval-rank helpers."""

import pytest

from eval.metrics import contains, normalise, percentile, retrieval_ranks


@pytest.mark.parametrize("raw,expected", [
    ("Drops after 9:00 PM", "drops after 21:00"),
    ("after 9 pm", "after 21:00"),
    ("after 9pm", "after 21:00"),
    ("after 9:00 p.m.", "after 21:00"),
    ("after 21:00", "after 21:00"),
    ("at 12 am", "at 00:00"),
    ("at 12 pm", "at 12:00"),
    ("₹1,000 per trip", "1000 per trip"),
    ("Rs. 1,000", "1000"),
    ("Rs 1000", "1000"),
    ("INR 2,500 a month", "2500 a month"),
    ("three no-shows", "3 no-shows"),
    ("  Lots\n of   SPACE ", "lots of space"),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


@pytest.mark.parametrize("text,needle", [
    ("Escort on drops departing after 9:00 PM.", "9 PM"),
    ("Escort on drops departing after 9:00 PM.", "9pm"),
    ("Escort after 21:00", "9:00 PM"),
    ("Escort after 9 pm", "21:00"),
    ("The cap is ₹1,000 per trip", "1000"),
    ("The cap is Rs 1000 per trip", "₹1,000"),
    ("The cap is INR 1,000", "1,000"),
    ("3 or more no-shows", "three"),
    ("three or more no-shows", "3 or more"),
    ("at least 2 hours before", "two hours"),
    ("The Transport Head approves", "transport head"),
])
def test_contains_matches_equivalents(text, needle):
    assert contains(text, needle)


@pytest.mark.parametrize("text,needle", [
    ("drops after 10:30 PM", "10 PM"),        # different time
    ("drops after 110 PM", "10 PM"),          # not a time at all
    ("drops after 11:00 PM", "1:00 PM"),      # 23:00 vs 13:00
    ("at least 12 hours before", "2 hours"),  # longer number
    ("cap of ₹11,000", "1,000"),              # longer amount
    ("cap of ₹1,0000", "1,000"),
    ("a 17-day suspension", "7-day"),
    ("drops after 9:00 PM", "9:00 AM"),
    ("drops after 9:00 PM", "8 PM"),
    ("someone will help", "1"),               # "one" inside a word is not a number
])
def test_contains_false_positive_guards(text, needle):
    assert not contains(text, needle)


def test_contains_empty_needle_never_matches():
    assert not contains("anything", "   ")


def test_retrieval_ranks():
    assert retrieval_ranks(["4", "2", "9"], ["4"]) == 1
    assert retrieval_ranks(["2", "9", "4"], ["4"]) == 3
    assert retrieval_ranks(["2", "9", "1"], ["4"]) is None
    assert retrieval_ranks(["3", "4", "2"], ["2", "3"]) == 1                    # any: first found
    assert retrieval_ranks(["3", "4", "2"], ["2", "3"], require_all=True) == 3  # all: last needed
    assert retrieval_ranks(["3", "4", "9"], ["2", "3"], require_all=True) is None
    assert retrieval_ranks(["3"], []) is None


def test_percentile():
    values = list(range(1, 101))
    assert percentile(values, 50) == pytest.approx(50.5)
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile([7.0], 95) == 7.0
