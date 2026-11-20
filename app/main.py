"""FastAPI app: routes, startup warm-up, and (later) uniform error handling."""

import logging
import time
from contextlib import asynccontextmanager

from app.embeddings import MiniLMEmbedder
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import load_settings
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
    embedder = MiniLMEmbedder()
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
    settings.validate()  # unsafe config stops startup here
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


app = FastAPI(title="Commute Policy Assistant", lifespan=lifespan)


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
