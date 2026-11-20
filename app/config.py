"""All tunables, read once from environment variables. No other module reads os.environ."""

import os
from dataclasses import dataclass
from pathlib import Path

# The fictional client companies. A company_id outside this set is rejected everywhere.
KNOWN_COMPANIES = frozenset({"kestrel", "tamarind", "orbitron"})
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Dev-only default so `docker compose up` works with no setup. Refused when DEV_MODE is off.
DEV_DEFAULT_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-000"
MIN_JWT_SECRET_LENGTH = 32


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str            # "groq" | "fake" (Gemini is a documented swap point, not implemented)
    groq_api_key: str
    llm_model: str
    llm_timeout_s: float
    llm_max_tokens: int          # completion budget; on gpt-oss it includes the hidden reasoning tokens
    llm_reasoning_effort: str    # gpt-oss: low | medium | high
    # Auth
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    jwt_ttl_s: int
    dev_mode: bool
    # Storage
    data_dir: Path
    policies_dir: Path
    # Retrieval / answering
    retrieval_k: int
    retrieval_mode: str          # "vector" | "hybrid"
    not_covered_min_sim: float
    retrieval_timeout_s: float
    request_timeout_s: float
    max_question_chars: int

    @property
    def llm_mode(self) -> str:
        """The LLM actually used: falls back to 'fake' when the chosen provider has no key."""
        if self.llm_provider == "groq" and self.groq_api_key:
            return "groq"
        return "fake"

    def validate(self) -> None:
        """Fail fast at startup on unsafe or impossible configuration."""
        if len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_LENGTH} characters")
        if not self.dev_mode and self.jwt_secret == DEV_DEFAULT_JWT_SECRET:
            raise ValueError("JWT_SECRET is the dev default; set a real secret when DEV_MODE is off")
        if not 1 <= self.jwt_ttl_s <= 24 * 3600:
            raise ValueError("JWT_TTL_S must be between 1 and 86400 seconds")
        if not 1 <= self.retrieval_k <= 3:
            raise ValueError("RETRIEVAL_K must be between 1 and 3 (max 3 cited chunks per answer)")
        if self.retrieval_mode not in {"vector", "hybrid"}:
            raise ValueError("RETRIEVAL_MODE must be 'vector' or 'hybrid'")
        if self.llm_provider not in {"groq", "fake"}:
            raise ValueError("LLM_PROVIDER must be 'groq' or 'fake' (Gemini is not implemented; see README)")
        if self.llm_reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("LLM_REASONING_EFFORT must be low, medium or high")


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        llm_provider=env("LLM_PROVIDER", "groq").strip().lower(),
        groq_api_key=env("GROQ_API_KEY", "").strip(),
        llm_model=env("LLM_MODEL", "openai/gpt-oss-20b"),   # checked 2026-11-20, docs/DESIGN_DECISIONS.md D57
        llm_timeout_s=float(env("LLM_TIMEOUT_S", "2.2")),
        llm_max_tokens=int(env("LLM_MAX_TOKENS", "300")),
        llm_reasoning_effort=env("LLM_REASONING_EFFORT", "low").strip().lower(),
        jwt_secret=env("JWT_SECRET", DEV_DEFAULT_JWT_SECRET),
        jwt_issuer=env("JWT_ISSUER", "commute-policy-mock-issuer"),
        jwt_audience=env("JWT_AUDIENCE", "commute-policy-api"),
        jwt_ttl_s=int(env("JWT_TTL_S", "900")),   # 15 min default token lifetime
        dev_mode=_bool("DEV_MODE", False),
        data_dir=Path(env("DATA_DIR", "./var")),
        policies_dir=Path(env("POLICIES_DIR", str(PROJECT_ROOT / "data" / "policies"))),
        retrieval_k=int(env("RETRIEVAL_K", "3")),
        retrieval_mode=env("RETRIEVAL_MODE", "vector").strip().lower(),
        not_covered_min_sim=float(env("NOT_COVERED_MIN_SIM", "0.15")),  # chosen on DEV, docs/DESIGN_DECISIONS.md D52
        retrieval_timeout_s=float(env("RETRIEVAL_TIMEOUT_S", "0.5")),
        request_timeout_s=float(env("REQUEST_TIMEOUT_S", "2.8")),
        max_question_chars=int(env("MAX_QUESTION_CHARS", "500")),
    )
