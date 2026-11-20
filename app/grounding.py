"""Checks on the model's answer, all plain code (D8, D10):

1. validate_citations: the model may only name labels S1..Sn that we gave it, 1-3 of them, and must
   name at least one when it answers.
2. unsupported_values: every number, time, money amount and duration in the answer must appear in the
   cited chunks' text or in the question. Uses app.textnorm, so "9 PM" == "9:00 PM" == "21:00" and
   "₹1,000" == "1000". The question counts as a source so that "my drop is at 9:30 PM" can be repeated.
3. foreign_companies: an answer that names another known company is not trusted.
4. render_citations: citation details come from chunk metadata, never from the model.
"""

import re

from app.config import KNOWN_COMPANIES
from app.retrieval import RetrievedChunk
from app.schemas import Citation
from app.textnorm import contains, normalise

MAX_CITATIONS = 3
SNIPPET_CHARS = 200

_LABEL = re.compile(r"\bs[1-9]\b")                    # "S1" in the answer is a label, not a number
_VALUE = re.compile(r"\d+(?::\d{2})?")                # after normalise(): 21:00, 1000, 5, 24 ...


class CitationError(ValueError):
    pass


def validate_citations(status: str, labels: list[str], available: int) -> list[str]:
    """Returns the de-duplicated labels, or raises CitationError."""
    if status == "not_covered":
        return []                                     # citations on a refusal are ignored
    unique = list(dict.fromkeys(labels))
    allowed = {f"S{i}" for i in range(1, available + 1)}
    if not unique:
        raise CitationError("answered without a citation")
    if len(unique) > MAX_CITATIONS:
        raise CitationError(f"{len(unique)} citations (max {MAX_CITATIONS})")
    unknown = [lab for lab in unique if lab not in allowed]
    if unknown:
        raise CitationError(f"unknown labels {unknown} (allowed {sorted(allowed)})")
    return unique


def values_in(text: str) -> list[str]:
    """Numbers/times/amounts in normalised form, e.g. "after 9 PM, ₹1,000" -> ["21:00", "1000"]."""
    return _VALUE.findall(_LABEL.sub(" ", normalise(text)))


def _twelve_hour(value: str) -> str | None:
    """'21:30' -> '9:30'. A question may say "9:30 at night" with no PM; the model writes "9:30 PM"."""
    if re.fullmatch(r"\d{2}:\d{2}", value) and 13 <= int(value[:2]) <= 23:
        return f"{int(value[:2]) - 12}:{value[3:]}"
    return None


def unsupported_values(answer: str, sources: list[str]) -> list[str]:
    """Values in the answer that no source contains (empty list = grounded)."""
    joined = "\n".join(sources)

    def supported(v: str) -> bool:
        alt = _twelve_hour(v)
        return contains(joined, v) or (alt is not None and contains(joined, alt))

    return [v for v in dict.fromkeys(values_in(answer)) if not supported(v)]


def strip_labels(answer: str) -> str:
    """Remove stray excerpt labels like "(S3)" from the answer text; citations are shown separately."""
    return re.sub(r"\s*[\(\[]\s*S[1-9](?:\s*,\s*S[1-9])*\s*[\)\]]", "", answer).strip()


def foreign_companies(answer: str, own_company: str) -> list[str]:
    text = answer.lower()
    return sorted(c for c in KNOWN_COMPANIES - {own_company} if re.search(rf"\b{c}\b", text))


def _body(chunk_text: str) -> str:
    """Chunk text minus its 'Section <id> <title>' first line."""
    return chunk_text.split("\n", 1)[1] if "\n" in chunk_text else chunk_text


def render_citations(labels: list[str], chunks: list[RetrievedChunk]) -> list[Citation]:
    out = []
    for label in labels:
        c = chunks[int(label[1:]) - 1]
        body = _body(c.text)
        snippet = body if len(body) <= SNIPPET_CHARS else body[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
        out.append(Citation(doc_id=c.doc_id, version=c.version, section_id=c.section_id,
                            section_title=c.section_title, snippet=snippet))
    return out
