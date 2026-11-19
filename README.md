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
