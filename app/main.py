"""FastAPI app: startup, routes, and the mapping from internal outcomes to HTTP responses.

Response shapes
- /ask (200 answered | 200 not_covered | 503 unavailable):
      {status, answer, citations, company_id, policy_version, request_id}
  For not_covered / unavailable, `answer` is the fixed text from app/messages.py and citations is [].
- Every other error (401, 403, 409, 413, 422, 500): {status, message, request_id}.
- `reason` (why not_covered) and failure causes never leave the server: logs and eval only (D60).
"""

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from app import messages
from app.ask import AskContext, QuestionInvalid, answer
from app.auth import AuthError, Principal, mint_token, require_principal
from app.config import load_settings
from app.embeddings import shared_embedder
from app.ingest import IngestFailed, PolicyParseError, ingest_policy, parse_policy, seed_if_empty
from app.llm import LLMUnavailable, build_llm
from app.middleware import RETRY_AFTER_S, BodyTooLarge, RequestContextMiddleware
from app.prompts import SYSTEM_PROMPT
from app.retrieval import RetrievalError, get_retriever
from app.schemas import AdminPolicyUpload, AskRequest, AskResult
from app.store import Store, VersionConflict

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
    if settings.retrieval_mode == "hybrid":   # build BM25 indexes now, so the first request doesn't pay
        t0 = time.perf_counter()
        built = get_retriever("hybrid").warm(store)
        log.info("bm25 indexes built: %d in %.0f ms", built, (time.perf_counter() - t0) * 1000)
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
    app.state.store = None
    app.state.system_prompt = SYSTEM_PROMPT
    app.state.result_sink = None     # eval hook: (request_id, AskResult | exception); None in normal runs
    if settings.dev_mode:
        log.warning("=" * 70)
        log.warning("DEV_MODE IS ON: /dev/token mints tokens for anyone. NEVER use in production.")
        log.warning("=" * 70)
    if settings.llm_mode == "fake":
        log.warning("No LLM API key set: using FakeLLM (extractive answers, pipeline check only).")
    else:
        log.info("LLM: %s model=%s timeout=%.1fs", settings.llm_mode, settings.llm_model, settings.llm_timeout_s)
    app.state.llm = build_llm(settings)
    try:
        start_store(app)
    except Exception:  # keep serving; /health reports not ready
        log.exception("startup failed; /health will report ready=false")
    yield
    store = app.state.store
    if store is not None:
        store.close()
        app.state.store = None
    close = getattr(app.state.llm, "close", None)
    if close is not None:
        close()


app = FastAPI(title="Commute Policy Assistant", lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)


# ---------------------------------------------------------------- error responses

class Forbidden(Exception):
    def __init__(self, message: str = messages.FORBIDDEN, reason: str = "role") -> None:
        super().__init__(reason)
        self.message, self.reason = message, reason


def rid(request: Request) -> str:
    return request.state.request_id


def error(request: Request, status_code: int, status: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"status": status, "message": message, "request_id": rid(request)},
                        status_code=status_code, headers=headers)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    # Same body for every reason, so the API never tells a caller WHY a token failed.
    # The reason is logged; the token and the Authorization header never are.
    log.warning("auth rejected request_id=%s path=%s reason=%s", rid(request), request.url.path, exc.reason)
    return error(request, 401, "auth_error", messages.AUTH_ERROR, {"WWW-Authenticate": "Bearer"})


@app.exception_handler(Forbidden)
async def forbidden_handler(request: Request, exc: Forbidden) -> JSONResponse:
    log.warning("forbidden request_id=%s path=%s reason=%s", rid(request), request.url.path, exc.reason)
    return error(request, 403, "forbidden", exc.message)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo the input back (FastAPI's default does): log only where and what kind of error.
    problems = [(".".join(str(p) for p in e.get("loc", ())), e.get("type")) for e in exc.errors()]
    log.info("bad request request_id=%s path=%s problems=%s", rid(request), request.url.path, problems)
    message = messages.BAD_QUESTION if request.url.path == "/ask" else messages.BAD_REQUEST
    return error(request, 422, "bad_request", message)


@app.exception_handler(BodyTooLarge)
async def too_large_handler(request: Request, exc: BodyTooLarge) -> JSONResponse:
    log.warning("413 request_id=%s path=%s (streamed body over limit)", rid(request), request.url.path)
    return error(request, 413, "payload_too_large", messages.PAYLOAD_TOO_LARGE)


# ---------------------------------------------------------------- health, dev token, whoami

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


# ---------------------------------------------------------------- /ask

def ask_body(result: AskResult, request_id: str) -> dict:
    return {
        "status": result.status,
        "answer": result.answer if result.status == "answered" else messages.NOT_COVERED,
        "citations": [c.model_dump() for c in result.citations],
        "company_id": result.company_id,
        "policy_version": result.policy_version,
        "request_id": request_id,
    }


def unavailable(principal: Principal, request_id: str, retry_after: float | None = None) -> JSONResponse:
    seconds = max(RETRY_AFTER_S, math.ceil(retry_after)) if retry_after else RETRY_AFTER_S
    body = {"status": "unavailable", "answer": messages.UNAVAILABLE, "citations": [],
            "company_id": principal.company_id, "policy_version": None, "request_id": request_id}
    return JSONResponse(body, status_code=503, headers={"Retry-After": str(seconds)})


@app.post("/ask")
async def ask(req: AskRequest, request: Request, principal: Principal = Depends(require_principal)) -> JSONResponse:
    """The whole pipeline runs under one deadline (REQUEST_TIMEOUT_S = 2.8 s). Its blocking steps
    (embedding + Chroma, the Groq call) already run in worker threads; on timeout the coroutine is
    cancelled and we answer 503 at once. A worker thread can't be killed: it finishes on its own (httpx's
    2.2 s timeout is per network phase, so a trickling server can keep it longer) and its result is thrown away."""
    request_id = rid(request)
    if app.state.store is None:
        return unavailable(principal, request_id)
    s = app.state.settings
    ctx = AskContext(app.state.store, app.state.llm, s, app.state.system_prompt)
    sink = app.state.result_sink or (lambda _rid, _outcome: None)
    try:
        result = await asyncio.wait_for(answer(principal, req.question, ctx, request_id), timeout=s.request_timeout_s)
    except QuestionInvalid:
        return error(request, 422, "bad_request", messages.BAD_QUESTION)
    except LLMUnavailable as exc:
        log.warning("503 request_id=%s company=%s cause=llm:%s", request_id, principal.company_id, exc.cause)
        sink(request_id, exc)
        return unavailable(principal, request_id, exc.retry_after)
    except RetrievalError as exc:
        log.warning("503 request_id=%s company=%s cause=retrieval:%s %s", request_id, principal.company_id,
                    type(exc).__name__, exc)
        sink(request_id, exc)
        return unavailable(principal, request_id)
    except asyncio.TimeoutError as exc:
        log.warning("503 request_id=%s company=%s cause=deadline %.1fs", request_id, principal.company_id,
                    s.request_timeout_s)
        sink(request_id, exc)
        return unavailable(principal, request_id)
    except Exception as exc:
        log.exception("503 request_id=%s company=%s cause=unhandled", request_id, principal.company_id)
        sink(request_id, exc)
        return unavailable(principal, request_id)
    sink(request_id, result)
    return JSONResponse(ask_body(result, request_id))


# ---------------------------------------------------------------- admin

def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if principal.role != "admin":
        raise Forbidden(reason=f"role={principal.role}")
    return principal


@app.post("/admin/policies", status_code=201)
async def upload_policy(req: AdminPolicyUpload, request: Request,
                        principal: Principal = Depends(require_admin)) -> JSONResponse:
    """Ingest a new policy version for the admin's OWN company, while the server keeps answering.

    The company, doc_id and version come from the document header. A document whose company_id is not
    the admin's company is refused (403) before anything is written."""
    request_id = rid(request)
    company = principal.company_id
    try:
        parsed = parse_policy(req.markdown)
    except PolicyParseError as exc:
        log.info("invalid policy request_id=%s company=%s: %s", request_id, company, exc)
        return error(request, 422, "invalid_policy", messages.INVALID_POLICY)
    if parsed.company_id != company:
        raise Forbidden(messages.OTHER_COMPANY, reason=f"document company={parsed.company_id!r}")
    if app.state.store is None:
        return error(request, 503, "unavailable", messages.UNAVAILABLE, {"Retry-After": str(RETRY_AFTER_S)})
    try:
        result = await asyncio.to_thread(ingest_policy, app.state.store, company, req.markdown)
    except PolicyParseError as exc:
        log.info("invalid policy request_id=%s company=%s: %s", request_id, company, exc)
        return error(request, 422, "invalid_policy", messages.INVALID_POLICY)
    except VersionConflict as exc:
        log.info("version conflict request_id=%s company=%s: %s", request_id, company, exc)
        return error(request, 409, "version_conflict", messages.VERSION_CONFLICT)
    except IngestFailed as exc:
        log.warning("ingest failed request_id=%s company=%s: %s", request_id, company, exc)
        return error(request, 500, "ingest_failed", messages.INGEST_FAILED)
    log.info("policy activated request_id=%s company=%s version=%s chunks=%s previous=%s", request_id, company,
             result.version, result.chunks, result.previous_version)
    return JSONResponse({"status": "active", "company_id": company, "doc_id": parsed.doc_id,
                         "version": result.version, "chunks": result.chunks,
                         "previous_version": result.previous_version, "request_id": request_id}, status_code=201)


@app.get("/admin/policies/status")
def policy_status(request: Request, principal: Principal = Depends(require_admin)) -> JSONResponse:
    store = app.state.store
    rows = store.rows(principal.company_id) if store is not None else []
    active = next((r for r in rows if r["status"] == "active"), None)
    return JSONResponse({
        "company_id": principal.company_id,
        "active_version": active["version"] if active else None,
        "doc_id": active["doc_id"] if active else None,
        "chunk_count": active["chunk_count"] if active else None,
        "activated_at": active["activated_at"] if active else None,
        "versions": [{"version": r["version"], "status": r["status"]} for r in rows],
        "request_id": rid(request),
    })
