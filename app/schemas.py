"""Pydantic models: the strict LLM output schema and the /ask response shape.

AskRequest (the HTTP body, extra=forbid) is added in P6 with the route.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr


class LLMOutput(BaseModel):
    """What we accept from the model. Unknown keys are ignored (e.g. a "reasoning" field); the known
    fields must be present with exactly these types, otherwise the output is malformed (D58)."""

    model_config = ConfigDict(extra="ignore", strict=True)
    status: Literal["answered", "not_covered"]
    answer: StrictStr
    citations: list[StrictStr]


class Citation(BaseModel):
    """Rendered by code from chunk metadata. The model only ever names a label like "S1"."""

    doc_id: str
    version: int
    section_id: str
    section_title: str
    snippet: str


# Internal reasons: logged and written to eval records, never shown to the user (D60).
Reason = Literal["below_threshold", "no_active_policy", "llm_not_covered", "bad_citation", "unverified"]


class AskResult(BaseModel):
    status: Literal["answered", "not_covered"]
    answer: str | None = None            # model text, only when answered and verified
    message: str | None = None           # fixed text from app/messages.py, only when not_covered
    citations: list[Citation] = []
    company_id: str
    policy_version: int | None
    # ---- internal (logs + eval only; the P6 HTTP response drops these) ----
    reason: Reason | None = None
    retrieved_sections: list[str] = []
    cited_labels: list[str] = []
    llm_calls: int = 0
    retrieve_ms: float = 0.0
    llm_ms: float = 0.0
    detail: str | None = None            # e.g. which number failed verification
