"""Policy ingestion: markdown -> section chunks; stage -> validate -> compare-and-swap flip -> cleanup.

A failed ingestion never changes which version answers: the old version stays active until the
single-transaction flip in Store.activate() succeeds.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import KNOWN_COMPANIES
from app.store import Store, VersionConflict, collection_name

log = logging.getLogger("app.ingest")

HEADER_RE = re.compile(r"^([a-z_]+):\s*(.+?)\s*$")
SECTION_RE = re.compile(r"^##\s+(\d+(?:\.\d+)*)\s+(.+?)\s*$")
REQUIRED_HEADER = ("company_id", "doc_id", "version")
MAX_CHUNK_CHARS = 800      # longer sections are split on paragraph boundaries
MAX_POLICY_CHARS = 200_000
MAX_VERSION = 1_000_000   # versions are small integers; a huge one overflowed SQLite (503, not 422)
ADD_BATCH_SIZE = 5         # chunks are written to Chroma in small batches


class PolicyParseError(ValueError):
    """The document is not a valid policy (bad header, no sections, duplicate IDs...)."""


class IngestFailed(RuntimeError):
    """Staging or validation failed; the previously active version is unchanged."""


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    text: str


@dataclass(frozen=True)
class ParsedPolicy:
    title: str
    company_id: str
    doc_id: str
    version: int
    sections: list[Section]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    metadata: dict


@dataclass(frozen=True)
class IngestResult:
    company_id: str
    version: int
    chunks: int
    previous_version: int | None


# ---------------------------------------------------------------- parsing (plain code)

def parse_policy(markdown: str) -> ParsedPolicy:
    if not markdown.strip():
        raise PolicyParseError("empty document")
    if len(markdown) > MAX_POLICY_CHARS:
        raise PolicyParseError("document too large")

    title, header, sections = "", {}, []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def close_section():
        if current is not None:
            sections.append(Section(current[0], current[1], "\n".join(body).strip()))

    for line in markdown.splitlines():
        if m := SECTION_RE.match(line):
            close_section()
            current, body = (m.group(1), m.group(2)), []
        elif current is not None:
            body.append(line)
        elif line.startswith("# ") and not title:
            title = line[2:].strip()
        elif m := HEADER_RE.match(line.strip()):
            header[m.group(1)] = m.group(2)
    close_section()

    missing = [k for k in REQUIRED_HEADER if k not in header]
    if missing:
        raise PolicyParseError(f"missing header fields: {missing}")
    try:
        version = int(header["version"])
    except ValueError as exc:
        raise PolicyParseError("version must be an integer") from exc
    if not 1 <= version <= MAX_VERSION:
        raise PolicyParseError(f"version must be between 1 and {MAX_VERSION}")
    if not sections:
        raise PolicyParseError("no '## <num> <title>' sections found")
    ids = [s.section_id for s in sections]
    if len(ids) != len(set(ids)):
        raise PolicyParseError("duplicate section IDs")
    if empty := [s.section_id for s in sections if not s.text]:
        raise PolicyParseError(f"empty sections: {empty}")

    return ParsedPolicy(title, header["company_id"].strip().lower(), header["doc_id"].strip(), version, sections)


def build_chunks(policy: ParsedPolicy) -> list[Chunk]:
    """One chunk per section; sections longer than MAX_CHUNK_CHARS split on blank lines."""
    chunks = []
    for s in policy.sections:
        parts, buf = [], ""
        for para in [p.strip() for p in s.text.split("\n\n") if p.strip()]:
            if buf and len(buf) + len(para) > MAX_CHUNK_CHARS:
                parts.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        parts.append(buf)
        for n, part in enumerate(parts, start=1):
            chunks.append(Chunk(
                chunk_id=f"{policy.doc_id}:v{policy.version}:s{s.section_id}:p{n}",
                # Heading is part of the text: helps retrieval and lets citations be checked.
                text=f"Section {s.section_id} {s.title}\n{part}",
                metadata={
                    "company_id": policy.company_id, "doc_id": policy.doc_id, "version": policy.version,
                    "section_id": s.section_id, "section_title": s.title,
                    "chunk_id": f"{policy.doc_id}:v{policy.version}:s{s.section_id}:p{n}",
                },
            ))
    return chunks


# ---------------------------------------------------------------- ingestion pipeline

def _stage(store: Store, name: str, chunks: list[Chunk]) -> None:
    col = store.create_collection(name)
    for i in range(0, len(chunks), ADD_BATCH_SIZE):
        batch = chunks[i:i + ADD_BATCH_SIZE]
        col.add(
            ids=[c.chunk_id for c in batch],
            documents=[c.text for c in batch],
            metadatas=[c.metadata for c in batch],
            embeddings=store.embedder([c.text for c in batch]),
        )


def post_validate(store: Store, name: str, chunks: list[Chunk], company_id: str, version: int) -> None:
    col = store.get_collection(name)
    if col.count() != len(chunks):
        raise IngestFailed(f"chunk count mismatch: stored {col.count()}, parsed {len(chunks)}")
    stored = col.get(include=["metadatas"])
    if any(m["company_id"] != company_id or m["version"] != version for m in stored["metadatas"]):
        raise IngestFailed("stored chunk metadata does not match company/version")
    probe = chunks[0]
    hit = col.query(query_embeddings=store.embedder([probe.text]), n_results=1)
    if hit["ids"][0] != [probe.chunk_id]:
        raise IngestFailed("smoke query did not return the expected chunk")


def ingest_policy(store: Store, company_id: str, markdown: str) -> IngestResult:
    """Ingest a new version for `company_id`. Raises PolicyParseError, VersionConflict or IngestFailed.

    The caller (API layer, P6) decides who may call this; here company_id is trusted input.
    """
    company_id = company_id.strip().lower()
    if company_id not in KNOWN_COMPANIES:
        raise PolicyParseError(f"unknown company: {company_id}")
    policy = parse_policy(markdown)                                   # 1. parse
    if policy.company_id != company_id:                               # 2. pre-validate
        raise PolicyParseError("document company_id does not match the target company")
    chunks = build_chunks(policy)
    sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    name = collection_name(company_id, policy.version)

    with store.company_lock(company_id):                              # one ingest per company at a time
        active = store.active_policy(company_id)
        previous = active.version if active else None
        if previous is not None and policy.version <= previous:
            raise VersionConflict(f"version {policy.version} is not newer than active version {previous}")
        store.create_staging_row(company_id, policy.doc_id, policy.version, sha)
        try:
            _stage(store, name, chunks)                               # 3. stage
            post_validate(store, name, chunks, company_id, policy.version)   # 4. post-validate
            store.activate(company_id, policy.version, len(chunks), expected_active=previous)  # 5. flip
        except Exception as exc:  # NOT BaseException: a real crash runs no cleanup; the janitor handles it
            store.drop_collection(name)
            store.mark_failed(company_id, policy.version, f"{type(exc).__name__}: {exc}")
            log.error("ingest failed company=%s version=%s: %s", company_id, policy.version, exc)
            if isinstance(exc, (VersionConflict, IngestFailed)):
                raise
            raise IngestFailed(str(exc)) from exc

    if active is not None:                                            # 6. cleanup (best effort)
        try:
            store.drop_collection(active.collection)
        except Exception:
            log.exception("could not delete old collection %s; janitor will retry", active.collection)
    log.info("ingested company=%s version=%s chunks=%d", company_id, policy.version, len(chunks))
    return IngestResult(company_id, policy.version, len(chunks), previous)


SEED_FILES = ("kestrel_v1.md", "tamarind_v1.md", "orbitron_v1.md")


def seed_if_empty(store: Store, policies_dir: Path) -> list[str]:
    """Ingest each seed policy whose company has no active version. Safe to call on every start."""
    seeded = []
    for filename in SEED_FILES:
        text = (policies_dir / filename).read_text(encoding="utf-8")
        company = parse_policy(text).company_id
        if store.active_policy(company) is None:
            ingest_policy(store, company, text)
            seeded.append(filename)
    return seeded
