# CLAUDE.md

Guidance for coding agents working in this repo.

## What this is
FastAPI + PostgreSQL service that receives helpdesk webhooks, drafts replies with Claude
using a small knowledge base, lets an operator approve/reject drafts, and delivers approved
replies back to the helpdesk. Design rationale: `docs/DESIGN.md`. Original task: `docs/TASK.md`.

## Layout
- `app/webhook.py` — `POST /webhooks/helpdesk`; must stay a single idempotent INSERT (2 s budget).
- `app/worker.py` — generation + delivery loops over `tickets` as a queue (SKIP LOCKED + leases).
- `app/llm.py` — Anthropic SDK call; no-key mode (real request → static draft, 5–30 s).
- `app/rate_limit.py` — global 60 req/min limiter in Postgres.
- `app/sender.py` — POST to `reply_url` with `Idempotency-Key`.
- `app/mock_helpdesk.py` — flaky helpdesk mock; `app/operator.py` — UI + JSON API; `app/kb.py` — BM25.
- `app/schema.sql` — idempotent schema applied at startup.

## Invariants (do not break)
- Webhook handler does no slow work and never calls the LLM.
- Every state write from a worker is guarded by `WHERE lease_id = $mine`.
- Every request to the LLM provider passes `rate_limit.acquire` first (SDK retries are off).
- One `idempotency_key` per ticket, reused on every delivery attempt.
- Operator transitions are conditional UPDATEs on the current status.

## Commands
```bash
docker compose up --build                       # app :8000 + worker + postgres
docker compose run --rm -e DATABASE_URL=postgresql://support:support@db:5432/support_test app pytest -q
docker compose exec app python scripts/simulate_helpdesk.py --count 100 --duration 30
```

## Conventions
- Raw SQL via asyncpg; no ORM. Cast numeric params explicitly in SQL (`$2::float8`) —
  Postgres infers `int` from expressions like `$2 * 5` and silently truncates.
- The Anthropic SDK 2.x uses `httpx2`; mock it in tests with `httpx2.MockTransport`, not respx.
- UI text is Ukrainian; code/comments in English.
