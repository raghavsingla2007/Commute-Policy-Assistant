"""FastAPI app: routes, startup warm-up, and (later) uniform error handling."""

import logging
import time
from contextlib import asynccontextmanager

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


def warm_up(app: FastAPI) -> None:
    """Load the embedding model and open Chroma so the first real request is not a cold start."""
    started = time.perf_counter()
    settings = app.state.settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    embedder = DefaultEmbeddingFunction()
    embedder(["warm-up"])  # forces the ONNX model to load
    client = chromadb.PersistentClient(
        path=str(settings.data_dir / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    client.heartbeat()
    app.state.embedder = embedder
    app.state.chroma = client
    app.state.ready = True
    log.info("warm-up done in %.0f ms", (time.perf_counter() - started) * 1000)


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
        warm_up(app)
    except Exception:  # keep serving; /health reports not ready
        log.exception("warm-up failed; /health will report ready=false")
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
