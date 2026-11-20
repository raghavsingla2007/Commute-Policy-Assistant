"""P2: registry + atomic ingestion + versioning + concurrency + storage-level isolation + restart."""

import threading
from pathlib import Path

import pytest


from app import ingest as ingest_mod
from app.embeddings import MiniLMEmbedder
from app.ingest import IngestFailed, PolicyParseError, ingest_policy, parse_policy, seed_if_empty
from app.store import Store, VersionConflict, collection_name

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"


def text(name: str) -> str:
    return (POLICIES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def embedder():
    return MiniLMEmbedder()  # real MiniLM, baked into the Docker image


@pytest.fixture
def store(tmp_path, embedder):
    return Store(tmp_path / "data", embedder)


def kestrel_chunks(store: Store) -> list[dict]:
    """Every chunk for kestrel across ALL pol_ collections (not just the active one)."""
    out = []
    for name in store.collection_names():
        got = store.get_collection(name).get(where={"company_id": "kestrel"}, include=["metadatas"])
        out += got["metadatas"]
    return out


def assert_v1_active_and_queryable(store: Store):
    active = store.active_policy("kestrel")
    assert active is not None and active.version == 1
    hits = store.search_active("kestrel", "security escort for night drops", k=3)
    assert hits and all(h["metadata"]["version"] == 1 for h in hits)
    assert any("10:00 PM" in h["text"] for h in hits)


# 1. Happy path -----------------------------------------------------------------------
def test_ingest_v1_happy_path(store):
    result = ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    assert (result.version, result.previous_version) == (1, None)
    parsed_count = len(parse_policy(text("kestrel_v1.md")).sections)
    assert result.chunks == parsed_count
    assert store.get_collection("pol_kestrel_v1").count() == parsed_count
    [row] = store.rows("kestrel")
    assert row["status"] == "active" and row["chunk_count"] == parsed_count


# 2. Upgrade --------------------------------------------------------------------------
def test_upgrade_flips_and_deletes_old_collection(store):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    result = ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    assert (result.version, result.previous_version) == (2, 1)
    assert {r["version"]: r["status"] for r in store.rows("kestrel")} == {1: "retired", 2: "active"}
    assert "pol_kestrel_v1" not in store.collection_names()
    assert {m["version"] for m in kestrel_chunks(store)} == {2}
    hits = store.search_active("kestrel", "security escort for night drops", k=3)
    assert any("9:00 PM" in h["text"] for h in hits)
    assert not any("10:00 PM" in h["text"] for h in hits)


# 3. Version <= active rejected --------------------------------------------------------
@pytest.mark.parametrize("second", ["kestrel_v1.md", "kestrel_v2.md"])
def test_version_not_newer_is_rejected(store, second):
    ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    with pytest.raises(VersionConflict):
        ingest_policy(store, "kestrel", text(second))
    assert store.active_policy("kestrel").version == 2
    assert store.get_collection("pol_kestrel_v2").count() == 10


def test_wrong_company_or_unknown_company_rejected(store):
    with pytest.raises(PolicyParseError, match="does not match"):
        ingest_policy(store, "tamarind", text("kestrel_v1.md"))
    with pytest.raises(PolicyParseError, match="unknown company"):
        ingest_policy(store, "acme", text("kestrel_v1.md"))
    assert store.rows() == []


# 4. Failure injection ----------------------------------------------------------------
class FailingEmbedder:
    """Works for the first `ok_calls` calls, then raises: simulates a failure mid-add."""

    def __init__(self, real, ok_calls):
        self.real, self.ok_calls, self.calls = real, ok_calls, 0

    def __call__(self, texts):
        self.calls += 1
        if self.calls > self.ok_calls:
            raise RuntimeError("embedding service exploded")
        return self.real(texts)


def assert_clean_failure(store: Store):
    assert_v1_active_and_queryable(store)
    assert "pol_kestrel_v2" not in store.collection_names()          # staging dropped
    statuses = {r["version"]: r["status"] for r in store.rows("kestrel")}
    assert statuses == {1: "active", 2: "failed"}


def test_embedder_fails_mid_add(store, embedder):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    store.embedder = FailingEmbedder(embedder, ok_calls=1)  # batch 1 of 2 written, batch 2 fails
    with pytest.raises(IngestFailed, match="exploded"):
        ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    store.embedder = embedder
    assert_clean_failure(store)


def test_post_validation_fails(store, monkeypatch):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))

    def bad_validate(*args, **kwargs):
        raise IngestFailed("chunk count mismatch (injected)")

    monkeypatch.setattr(ingest_mod, "post_validate", bad_validate)
    with pytest.raises(IngestFailed, match="injected"):
        ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    assert_clean_failure(store)


def test_failed_version_can_be_retried(store, embedder):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    store.embedder = FailingEmbedder(embedder, ok_calls=0)
    with pytest.raises(IngestFailed):
        ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    store.embedder = embedder
    assert ingest_policy(store, "kestrel", text("kestrel_v2.md")).version == 2


class SimulatedCrash(BaseException):
    """BaseException, like a killed process: the ingest's `except Exception` cleanup does NOT run."""


def test_crash_before_flip_then_janitor_on_restart(tmp_path, embedder, monkeypatch):
    store = Store(tmp_path / "data", embedder)
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))

    def crash(*args, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(store, "activate", crash)
    with pytest.raises(SimulatedCrash):
        ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    # Crashed state: orphan staging collection + staging row, but v1 still answers.
    assert "pol_kestrel_v2" in store.collection_names()
    assert {r["version"]: r["status"] for r in store.rows("kestrel")} == {1: "active", 2: "staging"}
    assert_v1_active_and_queryable(store)

    restarted = Store(tmp_path / "data", embedder)                    # "restart"
    report = restarted.janitor()
    assert report == {"stale_staging": 1, "orphans_deleted": ["pol_kestrel_v2"]}
    assert_clean_failure(restarted)


# 5. Concurrency ----------------------------------------------------------------------
def _race(store: Store, n: int = 2):
    barrier = threading.Barrier(n)
    outcomes = []

    def worker():
        barrier.wait()
        try:
            ingest_policy(store, "kestrel", text("kestrel_v2.md"))
            outcomes.append("ok")
        except VersionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(outcomes)


def assert_one_winner(store: Store, outcomes):
    assert outcomes == ["conflict", "ok"]
    active_rows = [r for r in store.rows("kestrel") if r["status"] == "active"]
    assert [r["version"] for r in active_rows] == [2]
    assert {m["version"] for m in kestrel_chunks(store)} == {2}


def test_concurrent_same_version_one_wins(store):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    assert_one_winner(store, _race(store))


def test_concurrent_without_lock_database_still_guards(store, monkeypatch):
    """Even with the in-process lock disabled, UNIQUE + compare-and-swap allow only one winner."""
    import contextlib

    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    monkeypatch.setattr(store, "company_lock", lambda company_id: contextlib.nullcontext())
    assert_one_winner(store, _race(store))


def test_compare_and_swap_rejects_stale_expected_version(store):
    ingest_policy(store, "kestrel", text("kestrel_v1.md"))
    ingest_policy(store, "kestrel", text("kestrel_v2.md"))
    store.create_staging_row("kestrel", "kestrel-transport-policy", 3, "x")
    with pytest.raises(VersionConflict, match="active version changed"):
        store.activate("kestrel", 3, chunk_count=1, expected_active=1)   # stale: active is 2
    assert store.active_policy("kestrel").version == 2


# 6. Storage-level isolation ----------------------------------------------------------
def test_collections_are_physically_separate(store):
    seed_if_empty(store, POLICIES)
    assert sorted(store.collection_names()) == ["pol_kestrel_v1", "pol_orbitron_v1", "pol_tamarind_v1"]
    other_canaries = {
        "kestrel": "Nimbus Rides helpline ext. 7720 Monsoon Cabs ext. 5523 interview candidates",
        "tamarind": "Falcon Fleet ext. 4411 Nimbus Rides Saturdays and Sundays Transport Head",
        "orbitron": "Falcon Fleet ext. 4411 Monsoon Cabs airport department head Facility Admin",
    }
    for company, bait in other_canaries.items():
        col = store.get_collection(collection_name(company, 1))
        got = col.get(include=["metadatas"])
        assert {m["company_id"] for m in got["metadatas"]} == {company}
        hits = store.search_active(company, bait, k=3)
        assert hits and all(h["metadata"]["company_id"] == company for h in hits)


# 7. Restart / seeding ----------------------------------------------------------------
def test_seed_is_idempotent_across_restarts(tmp_path, embedder):
    first = Store(tmp_path / "data", embedder)
    assert seed_if_empty(first, POLICIES) == ["kestrel_v1.md", "tamarind_v1.md", "orbitron_v1.md"]
    rows_before = first.rows()
    counts_before = {n: first.get_collection(n).count() for n in first.collection_names()}

    second = Store(tmp_path / "data", embedder)                       # restart
    assert second.janitor() == {"stale_staging": 0, "orphans_deleted": []}
    assert seed_if_empty(second, POLICIES) == []
    assert second.rows() == rows_before
    assert {n: second.get_collection(n).count() for n in second.collection_names()} == counts_before


def test_app_startup_seeds_once(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "appdata"))
    for _ in range(2):                                                # two app starts, same data dir
        with TestClient(app) as client:
            assert client.get("/health").json()["ready"] is True
            rows = app.state.store.rows()
    assert [(r["company_id"], r["version"], r["status"]) for r in rows] == [
        ("kestrel", 1, "active"), ("orbitron", 1, "active"), ("tamarind", 1, "active")]
