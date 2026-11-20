"""P3: the company comes ONLY from the verified token (Principal), never from the request.

Two independent checks, each with a self-test proving it can fail:
1. Runtime: walk every route's FastAPI dependency tree and list each input it reads from the request
   (query, header, cookie, path, body, including fields inside body models). None may mention "company",
   except /dev/token, which is the mock *issuer* and takes the company to put INTO a token.
2. Static: scan app/*.py for raw request access (request.headers, .query_params, .path_params, .cookies,
   await request.json()/body()/form()). Route code must use declared, typed inputs or the Principal.
"""

import ast
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.auth import require_principal
from app.main import app

APP_DIR = Path(__file__).resolve().parent.parent / "app"
ISSUER_ROUTES = {"/dev/token"}      # mints tokens; reading a company there is the point
RAW_ATTRS = {"headers", "query_params", "path_params", "cookies"}
RAW_CALLS = {"json", "body", "form", "stream"}


def _field_names(field) -> list[str]:
    names = [field.name, getattr(field, "alias", "") or ""]
    annotation = getattr(getattr(field, "field_info", None), "annotation", None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        names += list(annotation.model_fields)
    return names


def request_inputs(dependant) -> list[str]:
    """Every name a route (and its sub-dependencies) reads from the request."""
    out = []
    for group in ("path_params", "query_params", "header_params", "cookie_params", "body_params"):
        for field in getattr(dependant, group):
            out += [f"{group}:{n}" for n in _field_names(field) if n]
    for sub in dependant.dependencies:
        out += request_inputs(sub)
    return out


def company_inputs(target: FastAPI) -> dict[str, list[str]]:
    found = {}
    for route in target.routes:
        if isinstance(route, APIRoute) and route.path not in ISSUER_ROUTES:
            hits = [i for i in request_inputs(route.dependant) if "company" in i.lower()]
            if hits:
                found[route.path] = hits
    return found


def raw_request_access(source: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in RAW_ATTRS:
            hits.append(f"line {node.lineno}: .{node.attr}")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in RAW_CALLS and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"request", "req"}):
            hits.append(f"line {node.lineno}: {node.func.value.id}.{node.func.attr}()")
    return hits


# --- the real checks --------------------------------------------------------------------
def test_no_route_reads_company_from_the_request():
    assert company_inputs(app) == {}


def test_routes_were_actually_inspected():
    """Guard against a vacuous pass: the walker must see real inputs, e.g. /dev/token's body."""
    dev = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/dev/token")
    assert "body_params:company_id" in request_inputs(dev.dependant)
    who = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/whoami")
    assert [d.call for d in who.dependant.dependencies] == [require_principal]


def test_no_raw_request_access_in_app_source():
    hits = {p.name: raw_request_access(p.read_text(encoding="utf-8")) for p in sorted(APP_DIR.glob("*.py"))}
    assert {k: v for k, v in hits.items() if v} == {}


# --- self-tests: the checks catch planted violations ------------------------------------
def test_runtime_check_catches_planted_violations():
    bad = FastAPI()

    class Body_(BaseModel):
        question: str
        company_id: str

    @bad.get("/q")
    def from_query(company: str):
        return company

    @bad.get("/h")
    def from_header(x_company_id: str = Header()):
        return x_company_id

    @bad.post("/b")
    def from_body(req: Body_):
        return req

    def sneaky_dep(company_override: str = Body(None)):
        return company_override

    @bad.post("/d")
    def from_dependency(c=Depends(sneaky_dep)):
        return c

    assert sorted(company_inputs(bad)) == ["/b", "/d", "/h", "/q"]


def test_static_check_catches_planted_violations():
    planted = (
        "async def ask(request):\n"
        "    c = request.headers.get('x-company')\n"
        "    q = request.query_params['company']\n"
        "    body = await request.json()\n"
    )
    assert len(raw_request_access(planted)) == 3
