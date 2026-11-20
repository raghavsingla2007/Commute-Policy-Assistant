# Design decisions

The 20 decisions that shape the system. Numbers (D2, D14, …) are stable ids from the working log, so gaps
are expected. Each entry gives the decision, the alternatives considered, why this one, and what it costs.

## Isolation and versions

**D2 — One Chroma collection per (company, version), plus a SQLite pointer to the active one.**
- *Alternatives:* one collection filtered by `where` metadata.
- *Why:* other companies' and retired versions' chunks are not in the search space at all, so a filter bug
  can't leak them. Activating a version is one row change.
- *Trade-off:* more collections to create and clean up.

**D14 — Ingest = stage → validate → compare-and-swap flip → delete the old collection; a janitor runs at startup.**
- *Alternatives:* upsert into the live collection.
- *Why:* the flip is one SQLite transaction, so readers see v1 or v2, never half of each. A failure before
  the flip leaves v1 answering. A crash leaves an orphan collection, which the janitor deletes on the next
  start.
- *Trade-off:* a request that read the old pointer just before the flip finds its collection gone. It
  re-reads the registry once (D74) and answers from v2.

**D15 — Per-company lock and a compare-and-swap on the flip** (`UPDATE … WHERE status='active' AND version=:expected`, rowcount checked).
- *Alternatives:* lock only; a global lock.
- *Why:* the lock avoids wasted work in one process; the CAS is the real guarantee, and it holds across
  processes too.
- *Trade-off:* a little more code in the flip.

**D16 — Delete the old version's chunks after the flip; keep its registry row.**
- *Alternatives:* keep retired collections for rollback.
- *Why:* "once v2 is live, v1 never answers" then holds by construction. The row is kept for audit.
- *Trade-off:* no instant rollback; re-upload v1 as a higher version instead.

**D11 / D44 — Company only from the verified JWT.** The `/ask` body allows only `question` (`extra=forbid`).
Tests walk every route's dependencies and scan the source for request-derived company inputs.
- *Alternatives:* company in a header or body, validated against the token.
- *Why:* there is no override to abuse, and the tests stop a future route from quietly adding one. Each check
  has a planted-violation self-test.
- *Trade-off:* the checks are name-based, so a field called something unusual (e.g. `tenant`) would need
  adding.

**D51 — Assert every retrieved chunk's company and version; a mismatch is 503 "try again".**
- *Alternatives:* trust the per-company collection.
- *Why:* it catches my own bugs. A wrong-company answer is worse than no answer.
- *Trade-off:* an isolation bug looks like an outage to the user (it is logged loudly).

## Answering

**D3 — One LLM call per question (the brief allows two).**
- *Alternatives:* rewrite + answer; answer + judge.
- *Why:* it fits the 3 s p95 with room (measured p95 1,264 ms) and halves token use against the free tier's
  8,000 tokens/minute. Code does what a second call would check.
- *Trade-off:* no model-level second opinion.

**D9 / D52 — The LLM decides "not covered"; the similarity threshold (0.15) is only a cheap pre-filter.**
- *Alternatives:* a threshold-only gate.
- *Why:* on DEV, silent near-miss questions (holiday vs weekend rules) score as high as answerable ones
  (top-1 similarity 0.235–0.516 for both groups), so no threshold separates them; only reading does.
- *How the threshold was set:* the largest multiple of 0.05 at least 0.05 below the lowest DEV answerable
  top-1 (0.222).
- *Trade-off:* not-covered quality depends on the model. FakeLLM fails those cases.

**D8 — The model cites labels (S1..S3); code renders document, version and section from chunk metadata.**
- *Alternatives:* the model writes citation text.
- *Why:* the model can't invent a source or a section number.
- *Trade-off:* the model can still cite the wrong one of S1..S3 (partly caught by D10).

**D10 / D62 / D67 — Grounding verifier.**
- *What it checks:* every number, time, ₹ amount and duration in the answer must appear in the cited
  chunks or the question, after normalisation ("9 PM" = "21:00"). A value found only in the question needs a
  chunk-backed value beside it.
- *Alternatives:* LLM self-check; no check.
- *Why:* it is free and deterministic, and it blocks the most damaging error, a wrong value.
- *Trade-off:* false refusals on derived or oddly written values ("9 baje"). It does not check names or
  approvers.

**D13 — An answer that fails the citation or grounding checks becomes "not covered", not "try again".**
- *Alternatives:* a separate `unverified` status; 503.
- *Why:* at temperature 0 a retry gives the same answer, so "try again" would mislead. One user-facing state
  keeps clients simple.
- *Trade-off:* the internal reason has to be kept in logs and eval records (D60).

**D12 / D71 — Hard timeouts (0.5 s retrieval, 2.2 s LLM, 2.8 s overall), no retries inside a request.**
- *Alternatives:* retry on 429 or timeout.
- *Why:* a retry can't fit in 3 s; a fast, honest "try again" with `Retry-After` beats a slow answer.
- *Trade-off:* transient Groq blips reach the user. A timed-out worker thread finishes in the background.

**D57 — Groq `openai/gpt-oss-20b` over plain httpx.** JSON mode, temperature 0, `reasoning_effort=low`,
300 completion tokens.
- *Alternatives:* Llama 3.1 8B (planned); gpt-oss-120b; Qwen.
- *Why:* both Llama models returned 404 for this free-tier key (checked 2026-11-20). 20b is production-tier and
  answered in about 1 s; Qwen was faster but in preview. One HTTP call with an explicit timeout is easy to
  explain.
- *Trade-off:* hidden reasoning tokens count against the 300-token budget; a truncated reply is a 503.

**D60 — `reason` (why "not covered") stays internal.**
- *Alternatives:* return it to clients.
- *Why:* it describes pipeline internals and helps no user; the message is identical either way. The eval
  reads it through an internal hook.
- *Trade-off:* clients can't tell why; the `request_id` links to the server log.

## Platform and operations

**D5 / D24 — FakeLLM whenever there is no API key; FakeLLM is deliberately dumb.** It answers with the top
excerpt.
- *Alternatives:* require a key; a heuristic "smart" fake.
- *Why:* the 10-minute gate must never depend on a secret or the network. A clever fake would test the fake,
  not the system.
- *Trade-off:* the no-key container's answers are extractive, and its quality numbers are a pipeline check
  only.

**D31 / D32 — Own wrapper around Chroma's ONNX MiniLM that pads each text to its own length; we pass
embeddings to Chroma ourselves.**
- *Alternatives:* Chroma's default function (pads every text to 256 tokens); sentence-transformers.
- *Why:*
  - *Speed:* measured in Docker, query embedding went from ~490 ms to ~26 ms, about a sixth of the 3 s budget
    saved.
  - *Same vectors:* the output is identical (max difference ~1e-7, tested).
  - *One embedder:* a single embedder serves ingest and search, so they can't drift apart.
- *Trade-off:* it relies on a Chroma internal (`tokenizer`), which is pinned and guarded by tests.

**D40 / D41 — JWT rules.**
- *The rules:* HS256 pinned; `exp, iat, iss, aud, sub` required; company an exact string from an allowlist;
  role employee or admin; 10 s leeway; 15-minute tokens.
- *Failures:* every one returns the same 401 body, with the reason only in the server log.
- *Alternatives:* accept the algorithm the header names; normalise company strings; specific error messages.
- *Why:* this blocks `alg=none` and algorithm confusion. "Kestrel " can never alias a company, and an attacker
  learns nothing from the error.
- *Trade-off:* clients refresh every 15 minutes. HS256 is a mock-issuer choice; production would use RS256.

**D20 — `DEV_MODE` is off in code and on in `docker-compose.yml`, with a banner and a `/health` flag.**
- *What it does:* it enables `/dev/token`. With it off, the app refuses to start with the default JWT secret.
- *Alternatives:* always on; always off.
- *Why:* reviewers can mint a token with one curl, and production can't ship it by accident with a known
  secret.
- *Trade-off:* the compose file is demo-oriented by design.

## Evaluation

**D17 / D18 / D47 — Rule-based scoring; a DEV / HELD-OUT split, with HELD-OUT frozen by sha256 before any
tuning (`eval/FREEZE.md`, checked by a test).**
- *Alternatives:* LLM-as-judge; one undivided set.
- *Why:* scoring is reproducible, free and explainable. HELD-OUT gives an honest number on a small set that
  would otherwise be easy to overfit.
- *Trade-off:* phrase lists are brittle (two correct HELD-OUT answers are scored as failures), and n is
  small.

**D84 / D85 — Hybrid retrieval (BM25 + vector, RRF): one configuration and an adoption rule written before
any code, then applied literally.**
- *Alternatives:* a parameter sweep; adopt on the HELD-OUT gain.
- *Why:* every extra try on an eval set that was already used is tuning. Decided in advance, the comparison
  stays honest.
- *Result:* it lost on DEV (MRR 0.859 → 0.667) and broke one end-to-end case, so it is **not adopted**; the
  default stays vector. It is kept as `RETRIEVAL_MODE=hybrid` and fully tested.
- *Trade-off:* we don't know whether, say, a stopword list would have won.
