# Commute Policy Assistant

An API that answers employees' transport questions (night drops, cab cut-offs, no-shows, weekend pickups)
from **their own company's current policy only**, citing the document, version and section.
If the policy is silent, it says so and points to the transport desk instead of guessing.
Company comes only from the JWT; policy versions can be replaced live, and a failed upload never leaves a half-updated policy.
MoveInSync AI Developer case study B. Python 3.12, FastAPI, Chroma, MiniLM embeddings, Groq LLM, PyJWT.

## Quick start

```bash
docker compose up
```

That is the only command. The app starts on `http://localhost:8000`, seeds three fictional companies'
policies (Kestrel, Tamarind, Orbitron, all v1), and reports ready on `GET /health`.

The **first** run builds the image. It downloads Python packages and the embedding model (several hundred MB),
so it is network-bound: 8–11 minutes on a phone hotspot in testing. Later starts take seconds.

- **No API key needed.** Without a key the app uses **FakeLLM** and logs a warning. FakeLLM is a
  deterministic stand-in that answers with the top excerpt. It proves the pipeline works, but it can't judge
  when a policy is silent.
- **Real answers:** `cp .env.example .env`, set `GROQ_API_KEY=...` (free key from console.groq.com), then
  `docker compose up` again. `/health` then shows `"llm":"groq"`. The key is read from `.env` at runtime: it is
  never baked into the image, and `.env` is git- and docker-ignored.
- `docker-compose.yml` turns on `DEV_MODE`, which enables `POST /dev/token` (mints tokens for anyone) so you can
  try the API. Never run with `DEV_MODE=true` in production; without it the endpoint returns 404, and the app
  refuses to start with the default JWT secret.

## The three gate commands

```bash
docker compose up -d                                      # 1. start (builds the image on the first run)
until curl -sf localhost:8000/health; do sleep 2; done    #    wait: {"status":"ok","ready":true,"llm":"fake","dev_mode":true}
docker compose run --rm api pytest                        # 2. tests: offline, FakeLLM, no key needed
docker compose run --rm api python -m eval.run_eval       # 3. eval: all 55 cases over HTTP, FakeLLM, exit 0
```

The suite has 455 tests (one real-Groq test is skipped by default). It also passes with no network at all:
`docker run --rm --network none commute-policy-assistant:dev pytest`.

The gate eval runs with FakeLLM and writes `eval/results/fake_vector_all.jsonl` plus a `.summary.json`; FakeLLM passes
32/55 by design. The submitted evaluation set, `eval/eval_set.jsonl`, comes from the real Groq run instead (49/55, see
[Evaluation summary](#evaluation-summary)). To run with the real LLM:
`docker compose run --rm api python -m eval.run_eval --llm groq` (needs the key; paced at 8 s per call for the free
tier, about 8 minutes for 55 cases).

## Calling the API

Mint a token (DEV_MODE only) and ask a question. No `jq` needed:

```bash
TOKEN=$(curl -s -X POST localhost:8000/dev/token -H 'content-type: application/json' \
  -d '{"company_id":"kestrel","role":"employee"}' | sed -E 's/.*"token":"([^"]+)".*/\1/')

curl -s -X POST localhost:8000/ask -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question":"After what time do women get a security escort on night drops?"}'
```

Real responses with Groq (tokens and request ids shortened):

```text
200 answered (Kestrel, v1 active)
{"status":"answered",
 "answer":"Women employees receive a security escort on drops departing after 10:00 PM.",
 "citations":[{"doc_id":"kestrel-transport-policy","version":1,"section_id":"4","section_title":"Night Shift Safety",
               "snippet":"Women employees are provided a security escort on drops departing after 10:00 PM. …"}],
 "company_id":"kestrel","policy_version":1,"request_id":"80400fc4…"}

200 not_covered (Tamarind: "Can I book a cab on Saturday?"; the policy has no weekend rule)
{"status":"not_covered",
 "answer":"I couldn't find an answer to this in your company's transport policy. Please contact your transport desk for help.",
 "citations":[],"company_id":"tamarind","policy_version":1,"request_id":"cb585c08…"}

401 (no token, bad signature, expired, wrong company: always the same body)
{"status":"auth_error","message":"Your session is invalid or has expired. Please sign in again.","request_id":"e5ecf4ba…"}

422 ({"question":"escort?","company_id":"orbitron"}: a company in the body is rejected, never used)
{"status":"bad_request","message":"Please send a question as text, up to 500 characters.","request_id":"e1c86347…"}

503 (the LLM call failed or timed out; header Retry-After: 2)
{"status":"unavailable","answer":"We couldn't get an answer right now. Please try again in a moment.",
 "citations":[],"company_id":"kestrel","policy_version":null,"request_id":"1c6791b6…"}
```

The `/ask` body is `{"question": "..."}` and nothing else: 1–500 characters, 16 KiB body limit. `GET /whoami`
shows what the server reads from your token.

## Upgrading a policy live (admin API)

An admin uploads a whole policy file. Company, `doc_id` and version come from the document's own header, and
the company must be the admin's own. The server keeps answering throughout:

```bash
ADMIN=$(curl -s -X POST localhost:8000/dev/token -H 'content-type: application/json' \
  -d '{"company_id":"kestrel","role":"admin"}' | sed -E 's/.*"token":"([^"]+)".*/\1/')

docker compose exec -T api python -c 'import json; print(json.dumps({"markdown": open("data/policies/kestrel_v2.md").read()}))' \
  | curl -s -X POST localhost:8000/admin/policies -H "Authorization: Bearer $ADMIN" \
         -H 'content-type: application/json' --data-binary @-
# 201 {"status":"active","company_id":"kestrel","doc_id":"kestrel-transport-policy","version":2,"chunks":10,"previous_version":1,...}

curl -s localhost:8000/admin/policies/status -H "Authorization: Bearer $ADMIN"
# {"company_id":"kestrel","active_version":2,...,"versions":[{"version":1,"status":"retired"},{"version":2,"status":"active"}]}
```

The same escort question now answers "after 9:00 PM" with a version 2 citation. Other outcomes:
- 403: employee token, or a document claiming another company.
- 409: same or older version.
- 422: unreadable document.
- 500 "policy unchanged": failure while staging.
- 413: over 256 KiB.

To start again from v1, run `docker compose down -v && docker compose up`.

## Architecture

```
 client ──Bearer JWT + {question}──▶ FastAPI /ask            (middleware: request id, body limit, catch-all 503)
                                       │
                         [1] auth.py ──┤ company_id from the verified token ONLY
                         [2] schemas ──┤ validate the question (1–500 chars, no extra fields)
                         [3] store ────┤ SQLite registry: the company's ACTIVE collection
                                       ▼
                         [4] retrieval ── Chroma pol_<company>_v<active>, top 3 (0.5 s timeout)
                         [5] isolation assert: every chunk = caller's company + active version
                         [6] best similarity < 0.15? ──yes──▶ not_covered (0 LLM calls)
                                       │ no
                         [7] prompts ──┤ excerpts labelled S1..S3, quoted as data
                         [8] llm.py ───┤ ONE Groq call, 2.2 s timeout ──fail──▶ 503 "try again"
                         [9] parse ────┤ strict JSON schema ──malformed──▶ 503
                        [10] citations ┤ labels ⊆ {S1..S3}, 1–3 ──invalid──▶ not_covered
                        [11] grounding ┤ every number/time/₹ in cited text or question ──no──▶ not_covered
                        [12] render ───▶ 200 answered; citations built from chunk metadata

 admin JWT ──▶ /admin/policies ──▶ ingest.py: lock(company) → parse → stage new collection
                                    → validate → SQLite compare-and-swap flip → delete old collection
```

One container, with embedded Chroma (one collection per company and version) and a SQLite registry that says
which version is active. Startup runs the janitor (cleans up after crashes), seeds missing policies, and warms
the model and every active collection before `/health` reports ready.

### `/ask` request flow

| Step | Done by | Why it is code (or the LLM) |
|---|---|---|
| Verify JWT → company, role | code (`app/auth.py`) | authorisation must not depend on a model |
| Validate the question | code (`app/schemas.py`) | cheap, deterministic |
| Find the active version | code (`app/store.py`) | "only the live version answers" is a business rule |
| Embed and retrieve top 3 from that collection | code + MiniLM (`app/retrieval.py`) | search is not reasoning |
| Assert company and version of every chunk | code | defence against my own filter bugs |
| Pre-filter: top-1 similarity < 0.15 → not covered | code | skips the LLM for clearly unrelated questions |
| Decide answered / not covered, write the answer, cite labels | **LLM, one call** (`app/llm.py`) | the one step that needs reading: does this excerpt really answer it? |
| Parse, check citations, verify numbers, render citations | code (`app/llm.py`, `app/grounding.py`) | model output is untrusted |
| User-facing messages for not_covered / errors | code (`app/messages.py`) | never model-written |

## How we stay under 2 LLM calls and 3 cited chunks

**LLM calls.** The pipeline in `app/ask.py` contains exactly one LLM call, and no retries inside a request.
Before that call, a per-request `CallBudget` (`app/llm.py`) is spent. It raises `CallBudgetExceeded` on a third
call, so a future change can't silently exceed the brief's limit of 2. Questions caught by the pre-filter make
0 calls. Proof:
- `test_llm.py::test_budget_allows_two_and_raises_on_third` (the counter);
- `test_ask.py::test_exactly_one_llm_call_and_prompt_has_only_callers_excerpts` (the pipeline);
- `test_ask.py::test_below_threshold_makes_no_llm_call`;
- `test_api.py::test_eight_parallel_requests_one_llm_call_each` (budgets don't leak between concurrent requests);
- the eval records `llm_calls` per question: LLM calls per question, max 1 in every run.

**Cited chunks.** Retrieval returns `RETRIEVAL_K` chunks, and the app refuses to start unless 1 ≤ K ≤ 3. The
model only sees labels S1..SK and may cite 1–3 of them. Any other label, or more than 3, turns the answer into
"not covered". Citation details are rendered from chunk metadata, never written by the model. Proof:
- `test_health.py::test_retrieval_k_outside_1_to_3_is_refused`;
- `test_retrieval.py::test_k_comes_from_config`;
- `test_grounding.py::test_invalid_citations`;
- `test_ask.py::test_bad_citations_become_not_covered`.

## Design decisions (one line each)

The full list, with the alternatives considered, is in [`docs/DESIGN_DECISIONS.md`](docs/DESIGN_DECISIONS.md).

- **One Chroma collection per (company, version)** and a SQLite pointer to the active one. Other companies and
  old versions are not in the search space at all, and activation is one row change.
- **Stage → validate → compare-and-swap flip → delete old.** Readers see v1 or v2, never a mix. A failed or
  crashed upload leaves the old version answering.
- **Company only from the JWT.** The request body can't carry a company (`extra=forbid`), and tests scan every
  route for request-derived company inputs.
- **Isolation assertion after retrieval.** A wrong chunk is worse than no answer, so a mismatch returns "try again".
- **One LLM call; the LLM judges silence, and the similarity threshold only pre-filters.** On DEV, silent
  near-miss questions score as high as answerable ones, so only reading can separate them.
- **Labels-only citations + grounding verifier.** The model can't invent a source, and an answer with a number
  that isn't in the cited text is withheld.
- **Groq `openai/gpt-oss-20b` over plain httpx**: JSON mode, temperature 0, 2.2 s timeout, no retries. A retry
  can't fit in 3 s. The Llama models returned 404 for this free-tier key.
- **Own MiniLM wrapper** that pads each text to its own length instead of 256 tokens: identical vectors
  (tested), much faster queries.
- **Rule-based eval scoring** (phrase lists with a normaliser) and a frozen HELD-OUT split: reproducible, with
  no LLM judge.

## Security model

| Threat | Defence | Test |
|---|---|---|
| Forged token, `alg=none`, algorithm confusion, wrong issuer/audience, expired | HS256 pinned; `exp, iat, iss, aud, sub` required; 10 s leeway | `test_auth.py::test_bad_token_rejected` (21 cases) |
| Look-alike company in a token ("Kestrel ", "KESTREL") | exact match against an allowlist | `test_auth.py::test_bad_token_rejected[company_*]` |
| 401 tells an attacker why | one body for every failure; the reason goes to the server log only | `test_auth.py::test_every_auth_failure_has_identical_body` |
| Client picks another company | company only from the token; body allows only `question` | `test_principal_only.py` (route walk + source scan, with self-tests) |
| Cross-company retrieval | per-company collection + post-retrieval assertion → 503 | `test_retrieval.py::test_no_foreign_chunk_for_any_company_and_any_question`, `test_api.py::test_503_forced_isolation_violation` |
| Admin of company A uploads for company B | document company must equal the token's company, checked before any write | `test_admin.py::test_admin_of_kestrel_cannot_upload_a_document_claiming_orbitron` |
| Employee uses the admin API | `require_admin` | `test_admin.py::test_employee_cannot_upload_or_read_status` |
| Old version answers, or a mix during an upgrade | CAS flip; old collection deleted; one registry re-read if a collection vanishes mid-request | `test_admin.py::test_readers_during_the_flip_see_v1_or_v2_never_a_mix` |
| Half-updated policy after a failure or crash | stage → validate → flip; startup janitor | `test_store_ingest.py` (failure injection, crash + janitor) |
| Prompt injection in a policy or the question | excerpts quoted as data with `<>&` escaped; strict schema; citation check; verifier; foreign-company check | `test_prompts.py`, `test_ask.py`; eval `injection` cases |
| Invented citation or wrong number | labels only; verifier against the cited text | `test_grounding.py`, `test_ask.py::test_bad_citations_become_not_covered` |
| Internal details in errors | fixed messages; 422 never echoes input; catch-all 503 | `test_api.py::test_422_plain_message_no_echo`, `test_api.py::test_503_*` |
| Tokens or keys in logs | logs carry request id, company, status, reason, timings and the first 100 characters of the question; never tokens, keys or the Authorization header | `test_auth.py::test_reason_logged_but_token_never_logged` |
| Committed secrets | `.gitignore`/`.dockerignore` + secret-scan test | `test_no_secrets.py` |
| Oversized body | 413 over 16 KiB (`/ask`) or 256 KiB (`/admin`), declared or streamed | `test_api.py::test_413_*` |
| Dev token endpoint in production | off by default (404); refuses to start with the default secret | `test_auth.py::test_dev_token_is_404_when_dev_mode_off` |

## Failure handling

| What fails | User sees | Mechanism |
|---|---|---|
| Groq timeout, 429, 5xx, network, malformed or truncated output | 503 "try again", `Retry-After` | 2.2 s httpx timeout per network phase (connect, each read); no retry inside the request |
| Retrieval slow or errors | 503 "try again" | 0.5 s retrieval timeout |
| Whole request too slow | 503 "try again" | 2.8 s overall deadline (`asyncio.wait_for`) |
| Retrieved chunk from the wrong company or version | 503 "try again" | isolation assertion (logged loudly) |
| Model cites an unknown label, or a number isn't in the cited text | 200 not_covered | citation check, grounding verifier |
| Model says "answered" but its text is empty, whitespace or only a label like "(S1)" | 200 not_covered | treated as unverified (fixed after the audit; `test_ask.py::test_empty_answer_is_unverified_not_answered`) |
| Registry database locked (e.g. mid-upgrade) | 503 "try again" within the deadline | the registry read runs in a worker thread, so the 2.8 s deadline applies (`test_api.py::test_locked_registry_does_not_block_past_the_deadline`) |
| Policy upload fails mid-way | 500 "policy unchanged" | staging collection dropped; old version still active |
| Process crashes mid-upload | old version answers after restart | janitor fails the stale staging row, deletes orphans |
| Collection deleted by a concurrent upgrade | answer from the new version | re-read the registry once within the 0.5 s budget |
| Any unhandled exception | 503, no stack trace | middleware catch-all; traceback logged with the request id |

Timeouts in order: 0.5 s retrieval, 2.2 s LLM, 2.8 s overall. The 2.8 s deadline is the one the user feels. The 2.2 s httpx
timeout applies per network phase, so a server that trickles bytes can keep the worker thread busy longer (measured: 5.9 s).
The user still gets 503 at 2.8 s, and the thread's result is discarded.

## Evaluation summary

Full report: [`docs/EVAL_REPORT.md`](docs/EVAL_REPORT.md). Eval set (the brief's deliverable, six fields per line):
[`eval/eval_set.jsonl`](eval/eval_set.jsonl).

- **55 cases**: DEV 25, HELD-OUT 30. Categories: direct, paraphrase, cross-company, not-covered, near-miss,
  injection, leak bait, boundary, multi-section, Hinglish, post-version, and auth.
- HELD-OUT was frozen by hash before any tuning (`eval/FREEZE.md`).

Real Groq run, vector retrieval, one pass (`eval/results/groq_vector_full_p7_rescored.jsonl`):

| | Result |
|---|---|
| Pass | DEV 25/25, **HELD-OUT 24/30** (raw 22/30 before a scorer bug fix, see report) |
| Safety | leaks 0, unsupported claims 0 (no number shown to a user that isn't in the cited text or question) |
| Not covered | not-covered 18/18 correctly refused; false refusals 4/19 on HELD-OUT |
| Citations | citations correct 30/30 |
| Retrieval hit@1 · hit@3 · MRR (all 32 answerable) | 26/32 · 30/32 · 0.865 |
| LLM calls | LLM calls per question, max 1 |

Latency, `--repeat 3` (`eval/results/groq_vector_full_p7_latency.jsonl`):
- client wall clock p50 766 ms, **p95 1,264 ms**, max 2,148 ms over 161 successful responses;
- 0 rate-limit errors; one Groq timeout returned 503;
- **meets the 3 s p95 target.**

A second run at a busier time (`eval/results/groq_vector_full_p10_latency.jsonl`, `--repeat 2`) gave p95 1,705 ms
over the successful responses. 7 of 110 requests timed out with 503 "try again". No response took over 3 s, and
answer quality was unchanged.

Read these numbers with care:
- **n is small.** One case is 3–4 percentage points.
- **DEV is saturated at 25/25**, so it can no longer tell changes apart; HELD-OUT 24/30 is the honest number.
- **The 6 HELD-OUT failures are all safe:**
  - 4 refusals: multi-section 0/2 retrieval misses, one over-cautious refusal, one verifier false-reject on "9 baje";
  - 2 correct answers the phrase lists don't match.
  - No unsafe outcome.
- **The scorer is strict:** rule-based phrase lists, which undercount correct answers phrased differently.
- **FakeLLM numbers are a pipeline check only.** In the gate, the FakeLLM run passes 32/55, because FakeLLM
  can't judge silence.
- Measured from one machine, at one time of day, against Groq's shared free tier.

## Bonus: hybrid retrieval (tried, measured, not adopted)

BM25 keyword search fused with vector search by Reciprocal Rank Fusion: +48 lines in `app/retrieval.py` and
one dependency (`rank-bm25`). One configuration and an adoption rule were written down before any code. The
rule needed: MRR not lower, no hit@3 regression, under +50 ms p95, no new test failures or leaks, and no
end-to-end regression.

| | vector (default) | hybrid |
|---|---|---|
| hit@1 · hit@3 · MRR, all 32 answerable | 26/32 · 30/32 · 0.865 | 23/32 · 29/32 · 0.802 |
| MRR DEV / HELD-OUT | 0.859 / 0.868 | 0.859 → 0.667 / 0.868 → 0.895 |
| End to end with Groq, pass | DEV 25/25, HELD-OUT 24/30 | hybrid DEV 24/25, HELD-OUT 24/30 |
| Leaks / unsupported claims | 0 / 0 | 0 / 0 |

**Not adopted:**
- It lost on DEV: with no stopword list, BM25 rewards common words, so the No-show section rose for short
  questions, and one case lost its right section.
- It failed the rule's MRR and end-to-end clauses.
- It did not fix multi-section 0/2, the problem it was meant to help.

It stays available, fully tested for isolation and live upgrades, as `RETRIEVAL_MODE=hybrid docker compose up`.
Details: report §6.

## Trade-offs

1. **One LLM call plus code checks, instead of a second "judge" call.**
   - Gain: p95 stays at 1,264 ms, well under 3 s, and a model can't talk its way past citations, numbers or
     isolation.
   - Cost: no second opinion, so an over-cautious refusal goes uncaught.
2. **The LLM decides "not covered"; the threshold is set low.**
   - Gain: handles related-but-silent topics (holiday vs weekend rules) that embeddings can't separate.
   - Cost: not-covered quality depends on the model, and FakeLLM fails those cases.
3. **Unverifiable answers become "not covered".**
   - Gain: 0 unsupported numbers delivered.
   - Cost: occasional false refusals (e.g. "9 baje" in a Hinglish question).
4. **Delete the old version's chunks after a flip.**
   - Gain: "once v2 is live, v1 never answers" holds by construction.
   - Cost: no instant rollback; re-upload instead.
5. **Rule-based scoring.**
   - Gain: free, reproducible, explainable.
   - Cost: brittle to phrasing (2 correct HELD-OUT answers scored as failures).
6. **One pre-registered hybrid configuration instead of a sweep.**
   - Gain: an honest comparison on an eval set that was already used.
   - Cost: we don't know whether, say, a stopword list would have won.

## Limitations

- Answers are always in **English only**, also for Hinglish questions.
- Groq free tier: **8,000 tokens per minute**, about 8 questions per minute for the whole service, and 1,000
  requests per day. Beyond that users get "try again". Production needs a paid tier.
- Small corpus: 3 short fictional policies of about 10 sections each. Retrieval numbers won't transfer directly
  to 30-page policies.
- Multi-section questions are often refused, because one query embedding finds only the first topic.
- The verifier checks numbers, times, money and durations, not names: a wrong approver would not be caught.
- An answer that repeats a wrong value from the question passes the verifier if it also contains a value from
  the cited text.
- No personal eligibility: "Am I eligible?" gets the rule and its conditions.
- Single process. The ingest lock is in-process; the SQLite compare-and-swap is what would protect multiple
  processes.
- A timed-out worker thread can't be killed; it finishes in the background and its result is dropped.
- The JWT is HS256 with a shared secret (mock issuer); production would use RS256/ES256.
- The container runs as root, which avoids bind-mount permission failures in the gate.
- The eval's HTTP path is FastAPI's in-process TestClient, not a real network socket.
- **The verifier matches numbers, not units.** "Suspended for 2 days" passes against a chunk saying "2 no-shows …
  7 days", because the "2" appears in the cited text. The eval's unsupported-claim check uses the same matcher, so
  it shares this blind spot.
- Decimal values ("1.5 km") are always refused by the verifier (no current policy has decimals).
- Admin uploads are bounded only by size (256 KiB, 200,000 characters). The number of sections isn't capped: 500
  sections took 10 s to ingest. A single huge paragraph becomes one chunk.
- `/docs`, `/redoc` and `/openapi.json` stay enabled when `DEV_MODE` is off. They show route shapes only, nothing
  secret; production would disable them.
- `HEAD /health` returns 405 (use GET). The 2.8 s deadline starts after the request body has been read, so slow
  clients should be cut off by a reverse proxy.
- Tested on macOS arm64 (Docker via Colima). Not tested on x86_64.

## Troubleshooting

**Docker engine.**
- Any engine with Compose ≥ 2.24 works (Docker Desktop or Colima). Compose ≥ 2.24 is needed for the optional
  `.env`.
- With Colima, start it first (`colima start`).
- `docker compose version` should print a version.

**Eval results missing on the host (bind mount looks empty).**
- `./eval/results` is bind-mounted, so the host folder must be shared with the Docker VM.
- Colima shares only your home folder, so a checkout outside `~` (e.g. `/tmp`) gets an empty mount.
- Move the checkout under `~`, or add the path to Colima's mounts. Docker Desktop shares `/Users` and `/tmp` by
  default.

**`docker compose build` times out reaching PyPI** (`pip` "Read timed out", while `curl https://pypi.org` from
the same machine works).
- Some networks drop the larger post-quantum TLS hello that the image's OpenSSL 3.5 sends by default.
- Build with classic TLS key groups for the download steps only:

  ```bash
  CLASSIC_TLS=1 docker compose build && docker compose up
  ```

- The setting is off by default. It applies only inside the pip and model-download build step; the running
  container has no TLS override.
- On a phone hotspot the default build worked without it.

**Groq key and rate limits.**
- `/health` shows `"llm":"fake"`: the key wasn't picked up. Check that `.env` sits next to
  `docker-compose.yml` with `GROQ_API_KEY=...`, then restart.
- Repeated 503 "try again" with a key: you are over 8,000 tokens per minute or 1,000 requests per day. Wait a
  minute (the response's `Retry-After` gives a hint).
- `docker compose run --rm api python -m eval.run_eval --quota` prints the remaining daily requests (one tiny call).

**Port 8000 in use.** Stop the other service, or change the left side of `ports: "8000:8000"` in
`docker-compose.yml` (e.g. `"8080:8000"`) and use that port.

**DEV_MODE.**
- It is on in `docker-compose.yml` for review and demo only.
- With `DEV_MODE=false` you must set a real `JWT_SECRET` (≥ 32 characters), or the app refuses to start.
  `/dev/token` then returns 404; mint tokens with
  `docker compose run --rm api python -m scripts.issue_token --company kestrel --role employee`.

**Start fresh.** `docker compose down -v` deletes the data volume; the next `up` re-seeds v1.

## Project layout

```
app/
  main.py         routes, startup (janitor → seed → warm), error mapping
  ask.py          the /ask pipeline: read this first
  auth.py         JWT verify/mint, Principal (the only source of company)
  retrieval.py    vector + optional hybrid retrieval, timeout, isolation assertion, pre-filter
  llm.py          GroqLLM, FakeLLM, CallBudget, strict output parsing
  prompts.py      system prompt, excerpt labels
  grounding.py    citation checks, number verifier, citation rendering
  ingest.py       policy parser, stage → validate → flip
  store.py        SQLite registry, Chroma, janitor
  middleware.py   request id, body limits, catch-all 503
  config.py  schemas.py  messages.py  embeddings.py  textnorm.py
data/policies/    kestrel_v1.md  kestrel_v2.md  tamarind_v1.md  orbitron_v1.md (fictional)
eval/             cases.jsonl (inputs)  eval_set.jsonl (deliverable)  FREEZE.md  run_eval.py  metrics.py
eval/results/     committed real Groq runs (*.jsonl + *.summary.json)
docs/             EVAL_REPORT.md  DESIGN_DECISIONS.md  DEMO.md
scripts/          demo.sh  verify_fresh_clone.sh  check_docs_numbers.py  issue_token.py  ingest_cli.py
tests/            455 tests; conftest forces FakeLLM and an isolated data dir
```

`python -m scripts.check_docs_numbers` recomputes every number in this README and the report from
`eval/results/` and the test suite, and fails on any mismatch.

## Configuration

Every setting is an environment variable read once in `app/config.py`. `.env.example` lists them all. The
ones you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | empty | empty = FakeLLM |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Groq model id |
| `RETRIEVAL_MODE` | `vector` | `vector` or `hybrid` |
| `RETRIEVAL_K` | 3 | chunks given to the model (and the most it can cite); must be 1–3 |
| `NOT_COVERED_MIN_SIM` | 0.15 | pre-filter: below this top-1 similarity, answer "not covered" with no LLM call |
| `LLM_TIMEOUT_S` / `RETRIEVAL_TIMEOUT_S` / `REQUEST_TIMEOUT_S` | 2.2 / 0.5 / 2.8 | seconds |
| `DEV_MODE`, `JWT_SECRET`, `JWT_TTL_S` | off (on in compose), dev default, 900 | auth |

The 0.15 threshold was chosen on DEV data only (`docs/DESIGN_DECISIONS.md` D52).

## Switching the LLM provider

- **Model:** set `LLM_MODEL` in `.env` (default `openai/gpt-oss-20b`).
- **Provider:** Gemini is a documented swap point, not implemented. `app/llm.py` defines an `LLM` protocol
  with one method, `complete(prompt) -> str`. It returns the model's raw JSON text, or raises
  `LLMUnavailable(cause)`. To add Gemini:
  1. write a `GeminiLLM` class with that method (one HTTP call, JSON output, explicit timeout);
  2. add `"gemini"` to the provider check in `app/config.py` and a `GEMINI_API_KEY` setting;
  3. add one branch in `build_llm()`.

  Parsing, citation checks, the verifier, the call budget and the tests don't change.
