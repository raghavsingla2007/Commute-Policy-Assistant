"""JWT auth: the only place a caller's company comes from.

- `mint_token()` is the mock issuer, used by `POST /dev/token` (DEV_MODE only) and `scripts/issue_token.py`.
- `require_principal` is the FastAPI dependency every protected route uses. It returns a `Principal`.
  Downstream code gets the company ONLY from `principal.company_id`, never from the body, query or headers
  (enforced by tests/test_principal_only.py).
- Every failure raises `AuthError(reason)`. The API answers every AuthError with the same 401 body;
  the reason goes to the server log only. The token itself is never logged.
"""

import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import KNOWN_COMPANIES, Settings

ALGORITHM = "HS256"            # the only algorithm accepted; pinned in decode (no alg=none, no HS512, no RS*)
ROLES = frozenset({"employee", "admin"})
LEEWAY_S = 10                  # clock-skew allowance for exp/iat between issuer and API
MAX_TTL_S = 24 * 3600          # mint_token refuses longer-lived tokens


class AuthError(Exception):
    """Any authentication failure. `reason` is for the server log, never for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    company_id: str
    role: str
    subject: str


def _check_company(value: object) -> str:
    # Exact match only: no strip/lower/normalise. "Kestrel", "kestrel " and lists are all rejected.
    if not isinstance(value, str) or value not in KNOWN_COMPANIES:
        raise AuthError("company_id missing, not a string, or not in the allowlist")
    return value


def _check_role(value: object) -> str:
    if not isinstance(value, str) or value not in ROLES:
        raise AuthError("role missing or not one of employee/admin")
    return value


def mint_token(settings: Settings, company_id: str, role: str, subject: str = "demo-user",
               ttl_s: int | None = None) -> str:
    """Mock issuer. Validates its inputs with the same rules the verifier uses."""
    try:
        _check_company(company_id)
        _check_role(role)
    except AuthError as exc:
        raise ValueError(exc.reason) from None
    ttl = settings.jwt_ttl_s if ttl_s is None else ttl_s
    if not 1 <= ttl <= MAX_TTL_S:
        raise ValueError(f"ttl must be between 1 and {MAX_TTL_S} seconds")
    now = int(time.time())
    claims = {
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "sub": subject,
        "iat": now, "exp": now + ttl, "company_id": company_id, "role": role,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def verify_token(settings: Settings, token: str) -> Principal:
    try:
        claims = jwt.decode(
            token, settings.jwt_secret,
            algorithms=[ALGORITHM],                       # pinned: header "alg" cannot choose
            issuer=settings.jwt_issuer, audience=settings.jwt_audience,
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:                  # bad signature, alg, exp, iss, aud, format...
        raise AuthError(f"{type(exc).__name__}: {exc}") from None
    subject = claims["sub"]
    if not isinstance(subject, str) or not subject:
        raise AuthError("sub must be a non-empty string")
    return Principal(_check_company(claims.get("company_id")), _check_role(claims.get("role")), subject)


_bearer = HTTPBearer(auto_error=False)   # we raise our own uniform 401 instead of FastAPI's 403


def require_principal(request: Request,
                      creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise AuthError("missing or non-Bearer Authorization header")
    return verify_token(request.app.state.settings, creds.credentials)
