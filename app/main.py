"""FastAPI app: routes, startup warm-up, and (later) uniform error handling."""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from app import messages
from app.auth import AuthError, Principal, mint_token, require_principal
from app.config import load_settings
from app.embeddings import shared_embedder
from app.ingest import seed_if_empty
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


def start_store(app: FastAPI) -> None:
    """Open the store, clean up after any crash, seed v1 policies if missing, then warm everything.

    Order matters: janitor before seeding (so stale rows can't block a seed), warm-up last.
    """
    started = time.perf_counter()
    settings = app.state.settings
    embedder = shared_embedder()
    embedder(["warm-up"])  # forces the ONNX model to load
    store = Store(settings.data_dir, embedder)
    store.janitor()
    seeded = seed_if_empty(store, settings.policies_dir)
    if seeded:
        log.info("seeded policies: %s", seeded)
    store.warm()  # one query per active collection
    app.state.store = store
    app.state.ready = True
    log.info("startup (janitor + seed + warm-up) done in %.0f ms", (time.perf_counter() - started) * 1000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    try:
        settings.validate()  # unsafe config stops startup here
    except ValueError as exc:
        log.critical("REFUSING TO START: %s", exc)
        raise
    app.state.settings = settings
    app.state.ready = False
    if settings.dev_mode:
        log.warning("=" * 70)
        log.warning("DEV_MODE IS ON: /dev/token mints tokens for anyone. NEVER use in production.")
        log.warning("=" * 70)
    if settings.llm_mode == "fake":
        log.warning("No LLM API key set: using FakeLLM (extractive answers, pipeline check only).")
    try:
        start_store(app)
    except Exception:  # keep serving; /health reports not ready
        log.exception("startup failed; /health will report ready=false")
    yield
    store = getattr(app.state, "store", None)
    if store is not None:
        store.close()
        app.state.store = None


app = FastAPI(title="Commute Policy Assistant", lifespan=lifespan)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    # Same body for every reason, so the API never tells a caller WHY a token failed.
    # The reason is logged; the token and the Authorization header never are.
    request_id = uuid.uuid4().hex
    log.warning("auth rejected request_id=%s path=%s reason=%s", request_id, request.url.path, exc.reason)
    return JSONResponse(
        {"status": "auth_error", "message": messages.AUTH_ERROR, "request_id": request_id},
        status_code=401, headers={"WWW-Authenticate": "Bearer"},
    )


@app.get("/health")
def health() -> JSONResponse:
    s = app.state.settings
    body = {
        "status": "ok" if app.state.ready else "starting",
        "ready": app.state.ready,
        "llm": s.llm_mode,
        "dev_mode": s.dev_mode,
    }
    return JSONResponse(body, status_code=200 if app.state.ready else 503)


class DevTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: StrictStr
    role: StrictStr = "employee"
    subject: StrictStr = "demo-user"


@app.post("/dev/token")
def dev_token(req: DevTokenRequest) -> JSONResponse:
    """Mock login for demos. Exists only when DEV_MODE=true; otherwise it is indistinguishable from no route."""
    s = app.state.settings
    if not s.dev_mode:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    try:
        token = mint_token(s, req.company_id, req.role, req.subject)
    except ValueError as exc:
        return JSONResponse({"status": "bad_request", "message": str(exc)}, status_code=422)
    return JSONResponse({"token": token, "expires_in": s.jwt_ttl_s})


@app.get("/whoami")
def whoami(principal: Principal = Depends(require_principal)) -> dict:
    """What the server believes about the caller: only what the verified token says."""
    return {"company_id": principal.company_id, "role": principal.role, "subject": principal.subject}
