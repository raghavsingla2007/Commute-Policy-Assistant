"""SQLite policy registry, Chroma client helpers, and the startup janitor.

The registry is the source of truth for which version is live. Chroma only holds vectors:
one collection per (company, version), named pol_<company>_v<n>.
"""

import logging
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError

log = logging.getLogger("app.store")

Embedder = Callable[[list[str]], list]   # list of texts -> list of vectors

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_versions (
    id           INTEGER PRIMARY KEY,
    company_id   TEXT    NOT NULL,
    doc_id       TEXT    NOT NULL,
    version      INTEGER NOT NULL,
    collection   TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('staging', 'active', 'retired', 'failed')),
    sha256       TEXT    NOT NULL,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT    NOT NULL,
    activated_at TEXT,
    UNIQUE (company_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_company
    ON policy_versions (company_id) WHERE status = 'active';
"""


class VersionConflict(Exception):
    """The requested version is not newer than the active one, or another ingest won the race."""


@dataclass(frozen=True)
class ActivePolicy:
    company_id: str
    doc_id: str
    version: int
    collection: str


def collection_name(company_id: str, version: int) -> str:
    return f"pol_{company_id}_v{version}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir: Path, embedder: Embedder):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "registry.sqlite3"
        self.embedder = embedder
        self.chroma = chromadb.PersistentClient(
            path=str(data_dir / "chroma"), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ---------- SQLite ----------
    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN/COMMIT ourselves, so transactions are explicit.
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def company_lock(self, company_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[company_id]

    def active_policy(self, company_id: str) -> ActivePolicy | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT company_id, doc_id, version, collection FROM policy_versions "
                "WHERE company_id = ? AND status = 'active'",
                (company_id,),
            ).fetchone()
        return ActivePolicy(**dict(row)) if row else None

    def rows(self, company_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM policy_versions"
        args: tuple = ()
        if company_id:
            sql += " WHERE company_id = ?"
            args = (company_id,)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY company_id, version", args)]

    def create_staging_row(self, company_id: str, doc_id: str, version: int, sha256: str) -> None:
        """Insert the staging row. A previous *failed* attempt at the same version is replaced."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM policy_versions WHERE company_id = ? AND version = ? AND status = 'failed'",
                (company_id, version),
            )
            conn.execute(
                "INSERT INTO policy_versions (company_id, doc_id, version, collection, status, sha256, created_at) "
                "VALUES (?, ?, ?, ?, 'staging', ?, ?)",
                (company_id, doc_id, version, collection_name(company_id, version), sha256, _now()),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise VersionConflict(f"version {version} already exists for {company_id}") from exc
        finally:
            conn.close()

    def activate(self, company_id: str, version: int, chunk_count: int, expected_active: int | None) -> None:
        """The atomic flip: retire the expected old version and activate the new one in ONE transaction.

        Compare-and-swap: if the active version is no longer `expected_active`, nothing changes.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if expected_active is None:
                if conn.execute(
                    "SELECT 1 FROM policy_versions WHERE company_id = ? AND status = 'active'", (company_id,)
                ).fetchone():
                    raise VersionConflict("another version became active first")
            else:
                cur = conn.execute(
                    "UPDATE policy_versions SET status = 'retired' "
                    "WHERE company_id = ? AND status = 'active' AND version = ?",
                    (company_id, expected_active),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("the active version changed during ingestion")
            cur = conn.execute(
                "UPDATE policy_versions SET status = 'active', activated_at = ?, chunk_count = ? "
                "WHERE company_id = ? AND version = ? AND status = 'staging'",
                (_now(), chunk_count, company_id, version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("staging row missing")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def mark_failed(self, company_id: str, version: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE policy_versions SET status = 'failed', error = ? "
                "WHERE company_id = ? AND version = ? AND status = 'staging'",
                (error[:500], company_id, version),
            )

    # ---------- Chroma ----------
    def create_collection(self, name: str):
        self.drop_collection(name)  # remove a leftover from an earlier failed attempt
        return self.chroma.create_collection(
            name, embedding_function=None, configuration={"hnsw": {"space": "cosine"}}
        )

    def get_collection(self, name: str):
        return self.chroma.get_collection(name, embedding_function=None)

    def drop_collection(self, name: str) -> None:
        try:
            self.chroma.delete_collection(name)
        except NotFoundError:
            pass

    def collection_names(self) -> list[str]:
        return [c.name for c in self.chroma.list_collections()]

    def search_active(self, company_id: str, text: str, k: int) -> list[dict]:
        """Top-k chunks from the company's ACTIVE collection only. Retrieval logic builds on this in P4."""
        active = self.active_policy(company_id)
        if active is None:
            return []
        col = self.get_collection(active.collection)
        res = col.query(
            query_embeddings=self.embedder([text]), n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        return [
            {"id": i, "text": d, "metadata": m, "distance": dist}
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]

    # ---------- Startup ----------
    def janitor(self) -> dict:
        """Run at startup, before serving: fail stale staging rows, delete unreferenced collections."""
        with self._connect() as conn:
            stale = conn.execute(
                "UPDATE policy_versions SET status = 'failed', error = 'stale staging row (janitor)' "
                "WHERE status = 'staging'"
            ).rowcount
            live = {r["collection"] for r in conn.execute(
                "SELECT collection FROM policy_versions WHERE status = 'active'")}
        orphans = [n for n in self.collection_names() if n.startswith("pol_") and n not in live]
        for name in orphans:
            self.drop_collection(name)
        if stale or orphans:
            log.warning("janitor: %d stale staging rows failed, orphans deleted: %s", stale, orphans)
        return {"stale_staging": stale, "orphans_deleted": orphans}

    def warm(self) -> None:
        """Touch every active collection once so the first real query is not cold."""
        with self._connect() as conn:
            names = [r["collection"] for r in conn.execute(
                "SELECT collection FROM policy_versions WHERE status = 'active'")]
        vec = self.embedder(["warm-up"])
        for name in names:
            self.get_collection(name).query(query_embeddings=vec, n_results=1)
