# Commute Policy Assistant

Answers employee transport questions from **their own company's current policy**, with document + section citations,
an explicit "not covered" reply, and a JWT-scoped API. MoveInSync AI Developer case study B.

## Start (one command)

```bash
docker compose up
```

No API key is needed: without `GROQ_API_KEY` the app uses FakeLLM (pipeline check only).
For real answers: `cp .env.example .env`, set `GROQ_API_KEY`, then `docker compose up`.

> `docker-compose.yml` enables `DEV_MODE` (a `/dev/token` endpoint that mints tokens) for review and demo.
> It must be **off** in production.

## The 10-minute gate

```bash
docker compose up                                        # 1. start; GET http://localhost:8000/health
docker compose run --rm api pytest                       # 2. tests (offline, FakeLLM)
docker compose run --rm api python -m eval.run_eval      # 3. evaluation set
```

Design, decisions and trade-offs: see `PROJECT_LOG.md` (README sections are filled in P9).

## LLM provider

- Default: Groq, model `openai/gpt-oss-20b` (`LLM_MODEL`), JSON mode, temperature 0, one call per question,
  2.2 s timeout, no retries. On the free tier checked on 2026-11-20 the Llama models returned 404 for this key.
- Real answers with the eval: `docker compose run --rm api python -m eval.run_eval --llm groq --split dev`.
- **Swapping to Gemini (not implemented):** `app/llm.py` defines an `LLM` protocol with one method,
  `complete(prompt) -> str` (the model's raw JSON text, or raise `LLMUnavailable(cause)`). A `GeminiLLM` class
  implementing it, plus one branch in `build_llm()` and the key in `app/config.py`, is the whole change; parsing,
  citation checks and the grounding verifier don't change.
- Answers are always in English, also for Hinglish questions.

## Docker runtime

Any Docker engine with Compose ≥ 2.24 works: Docker Desktop or Colima. Colima shares only your home folder with its VM,
so if the project sits outside `~`, the `./eval/results` bind mount looks empty from inside the container. That is a
local-machine quirk, not a product limitation.
