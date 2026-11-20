"""P3: JWT verification, uniform 401, DEV_MODE token endpoint, refuse-to-start, mock issuer CLI."""

import base64
import json
import logging
import time
import warnings

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import LEEWAY_S, AuthError, mint_token, verify_token
from app.config import DEV_DEFAULT_JWT_SECRET, load_settings
from app.main import app
from scripts import issue_token

STRONG_SECRET = "test-secret-" + "x" * 40


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """One app start for the whole module (each start opens a store; ~1 s and file handles)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LLM_PROVIDER", "fake")
        mp.delenv("GROQ_API_KEY", raising=False)
        mp.delenv("GEMINI_API_KEY", raising=False)
        mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("authdata")))
        mp.setenv("DEV_MODE", "true")
        mp.delenv("JWT_SECRET", raising=False)
        mp.delenv("JWT_TTL_S", raising=False)
        with TestClient(app) as c:
            yield c


def settings_of(client):
    return client.app.state.settings


def claims(client, **overrides):
    s = settings_of(client)
    now = int(time.time())
    base = {"iss": s.jwt_issuer, "aud": s.jwt_audience, "sub": "u1", "iat": now, "exp": now + 300,
            "company_id": "kestrel", "role": "employee"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not DROP}


DROP = object()   # marker: remove this claim


def sign(client, payload, secret=None, alg="HS256"):
    return jwt.encode(payload, secret or settings_of(client).jwt_secret, algorithm=alg)


def whoami(client, token):
    return client.get("/whoami", headers={"Authorization": f"Bearer {token}"})


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


# --- happy path --------------------------------------------------------------------------
def test_valid_token_gives_principal(client):
    resp = whoami(client, sign(client, claims(client, sub="alice")))
    assert resp.status_code == 200
    assert resp.json() == {"company_id": "kestrel", "role": "employee", "subject": "alice"}


# --- 1. algorithm and standard claims --------------------------------------------------
def bad_tokens(client):
    now = int(time.time())
    with warnings.catch_warnings():   # PyJWT warns the key is short for HS512; expected here
        warnings.simplefilter("ignore")
        hs512 = sign(client, claims(client), alg="HS512")
    return {
        "alg_none": _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(claims(client)) + ".",
        "hs512_right_secret": hs512,                                       # algorithm confusion
        "wrong_secret": sign(client, claims(client), secret="another-secret-" + "y" * 40),
        "expired": sign(client, claims(client, exp=now - LEEWAY_S - 30)),
        "missing_exp": sign(client, claims(client, exp=DROP)),
        "missing_iss": sign(client, claims(client, iss=DROP)),
        "wrong_iss": sign(client, claims(client, iss="someone-else")),
        "wrong_aud": sign(client, claims(client, aud="another-api")),
        "missing_sub": sign(client, claims(client, sub=DROP)),
        "missing_company": sign(client, claims(client, company_id=DROP)),
        # 2. company_id: exact string match against the allowlist, no normalising
        "company_capitalised": sign(client, claims(client, company_id="Kestrel")),
        "company_trailing_space": sign(client, claims(client, company_id="kestrel ")),
        "company_path": sign(client, claims(client, company_id="kestrel/../orbitron")),
        "company_list": sign(client, claims(client, company_id=["kestrel", "orbitron"])),
        "company_int": sign(client, claims(client, company_id=1)),
        "company_unknown": sign(client, claims(client, company_id="acme")),
        # 3. role
        "role_unknown": sign(client, claims(client, role="superadmin")),
        "role_capitalised": sign(client, claims(client, role="Admin")),
        "role_list": sign(client, claims(client, role=["admin"])),
        "role_missing": sign(client, claims(client, role=DROP)),
        # format
        "garbage": "not.a.jwt",
    }


BAD_NAMES = ["alg_none", "hs512_right_secret", "wrong_secret", "expired", "missing_exp", "missing_iss",
             "wrong_iss", "wrong_aud", "missing_sub", "missing_company", "company_capitalised",
             "company_trailing_space", "company_path", "company_list", "company_int", "company_unknown",
             "role_unknown", "role_capitalised", "role_list", "role_missing", "garbage"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_token_rejected(client, name):
    resp = whoami(client, bad_tokens(client)[name])
    assert resp.status_code == 401, name


def test_bad_token_list_is_complete(client):
    assert sorted(bad_tokens(client)) == sorted(BAD_NAMES)


def test_verify_token_rejects_directly(client):
    """Same checks without HTTP, so a failure points at auth.py, not at routing."""
    s = settings_of(client)
    for name, token in bad_tokens(client).items():
        with pytest.raises(AuthError):
            verify_token(s, token)


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "Token abc"])
def test_missing_or_non_bearer_header_rejected(client, header):
    headers = {} if header is None else {"Authorization": header}
    assert client.get("/whoami", headers=headers).status_code == 401


# --- 4. uniform 401, reason only in the log --------------------------------------------
def test_every_auth_failure_has_identical_body(client):
    bodies = set()
    responses = [whoami(client, t) for t in bad_tokens(client).values()]
    responses.append(client.get("/whoami"))
    for resp in responses:
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        body = resp.json()
        assert body.pop("request_id")                     # present, differs per request
        bodies.add(json.dumps(body, sort_keys=True))
    assert bodies == {json.dumps({"message": "Your session is invalid or has expired. Please sign in again.",
                                  "status": "auth_error"}, sort_keys=True)}


def test_reason_logged_but_token_never_logged(client, caplog):
    token = bad_tokens(client)["wrong_iss"]
    with caplog.at_level(logging.DEBUG):
        whoami(client, token)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "auth rejected" in logs and "InvalidIssuerError" in logs
    assert token not in logs
    for part in token.split("."):
        assert part not in logs
    assert settings_of(client).jwt_secret not in logs


# --- 5. leeway and lifetime -------------------------------------------------------------
def test_small_clock_skew_is_tolerated(client):
    now = int(time.time())
    token = sign(client, claims(client, exp=now - (LEEWAY_S - 5)))   # expired 5 s ago, within leeway
    assert whoami(client, token).status_code == 200


def test_default_token_lifetime_is_short(client):
    s = settings_of(client)
    assert s.jwt_ttl_s == 900
    decoded = jwt.decode(mint_token(s, "kestrel", "employee"), options={"verify_signature": False})
    assert decoded["exp"] - decoded["iat"] == 900


def test_mint_token_validates_inputs(client):
    s = settings_of(client)
    for company, role, ttl in [("Kestrel", "employee", None), ("kestrel", "root", None),
                               ("kestrel", "employee", 0), ("kestrel", "employee", 86401)]:
        with pytest.raises(ValueError):
            mint_token(s, company, role, ttl_s=ttl)


# --- 6. refuse to start -----------------------------------------------------------------
@pytest.mark.parametrize("secret,match", [(DEV_DEFAULT_JWT_SECRET, "dev default"), ("short-secret", "at least 32")])
def test_refuses_to_start_with_unsafe_secret_when_dev_mode_off(monkeypatch, caplog, secret, match):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JWT_SECRET", secret)
    with pytest.raises(ValueError, match=match):
        with TestClient(app):
            pass
    assert "REFUSING TO START" in caplog.text
    assert secret not in caplog.text


def test_short_secret_refused_even_in_dev_mode(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short-secret")
    with pytest.raises(ValueError, match="at least 32"):
        with TestClient(app):
            pass


# --- 7. /dev/token and the CLI ----------------------------------------------------------
def test_dev_token_works_when_dev_mode_on(client):
    resp = client.post("/dev/token", json={"company_id": "orbitron", "role": "admin"})
    assert resp.status_code == 200 and resp.json()["expires_in"] == 900
    assert whoami(client, resp.json()["token"]).json() == {
        "company_id": "orbitron", "role": "admin", "subject": "demo-user"}


@pytest.mark.parametrize("body", [{"company_id": "Kestrel"}, {"company_id": "acme"},
                                  {"company_id": "kestrel", "role": "root"},
                                  {"company_id": ["kestrel"]}, {"company_id": "kestrel", "extra": 1}])
def test_dev_token_rejects_bad_input(client, body):
    assert client.post("/dev/token", json=body).status_code == 422


def test_dev_token_is_404_when_dev_mode_off(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JWT_SECRET", STRONG_SECRET)
    with TestClient(app) as c:
        resp = c.post("/dev/token", json={"company_id": "kestrel", "role": "employee"})
        assert resp.status_code == 404
        assert resp.json() == c.post("/no-such-route").json()       # looks exactly like a missing route


def test_issue_token_cli(monkeypatch, capsys):
    monkeypatch.setenv("JWT_SECRET", STRONG_SECRET)
    assert issue_token.main(["--company", "tamarind", "--role", "admin", "--ttl", "60", "--subject", "ops"]) == 0
    token = capsys.readouterr().out.strip()
    principal = verify_token(load_settings(), token)
    assert (principal.company_id, principal.role, principal.subject) == ("tamarind", "admin", "ops")
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["exp"] - decoded["iat"] == 60


def test_issue_token_cli_rejects_bad_input(monkeypatch, capsys):
    monkeypatch.setenv("JWT_SECRET", STRONG_SECRET)
    for argv in (["--company", "Kestrel"], ["--company", "kestrel", "--role", "root"]):
        with pytest.raises(SystemExit) as exc:
            issue_token.main(argv)
        assert exc.value.code == 2                               # argparse choices
    assert issue_token.main(["--company", "kestrel", "--ttl", "0"]) == 1
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JWT_SECRET", DEV_DEFAULT_JWT_SECRET)
    assert issue_token.main(["--company", "kestrel"]) == 1         # same refuse rule as the server
