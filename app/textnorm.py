"""Text normalisation shared by the grounding verifier (app/grounding.py) and the eval scorer.

normalise() makes equivalent spellings compare equal:
  - lowercase, collapse whitespace
  - times: "9 pm", "9pm", "9:00 pm", "9:00 p.m." and "21:00" all become "21:00"
  - money: "₹1,000", "Rs. 1000", "INR 1,000" and "1000" all become "1000"
  - small counts: "one".."ten" become "1".."10" ("three" == "3")
contains() then does a substring match, except that a needle starting or ending with a digit
must not continue a longer number in the text ("2 hours" does not match "12 hours", "10 pm" does
not match "110 pm" or "10:30 pm").
"""

import re

_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}

_CURRENCY = re.compile(r"₹\s*|\b(?:rs\.?|inr)\s*(?=\d)")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 12-hour time with am/pm: 9 pm, 9pm, 9:00 pm, 9:00 p.m.  (\b stops "110 pm" from matching "10 pm")
_TIME_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?")
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b")
_SPACES = re.compile(r"\s+")


def _to_24h(match: re.Match) -> str:
    hour, minute, half = int(match.group(1)), match.group(2) or "00", match.group(3)
    if not 1 <= hour <= 12:
        return match.group(0)            # "13 pm" is not a time; leave it alone
    if half == "p" and hour != 12:
        hour += 12
    if half == "a" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"


def normalise(text: str) -> str:
    text = text.lower()
    text = _CURRENCY.sub("", text)
    text = _THOUSANDS.sub("", text)
    text = _TIME_12H.sub(_to_24h, text)
    text = _NUMBER_WORD.sub(lambda m: _NUMBER_WORDS[m.group(1)], text)
    return _SPACES.sub(" ", text).strip()


def contains(text: str, needle: str) -> bool:
    hay, pin = normalise(text), normalise(needle)
    if not pin:
        return False
    before = r"(?<![\d:.,])" if pin[0].isdigit() else ""
    after = r"(?![\d:])" if pin[-1].isdigit() else ""
    return re.search(before + re.escape(pin) + after, hay) is not None
