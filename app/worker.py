"""Background worker: two loops over the tickets table used as a durable queue.

* generation: new -> generating -> draft_ready (or retry / gen_failed)
* delivery:   approved -> sending -> sent      (or retry / send_failed)

Rows are claimed with UPDATE ... FOR UPDATE SKIP LOCKED and a lease (lease_id,
lease_until). If a worker dies, the lease expires and another worker reclaims the
row — nothing is lost. Results are written only `WHERE lease_id = <mine>`, so a
worker whose lease was taken over can't overwrite the newer owner's result.

Run:  python -m app.worker   (any number of processes; the LLM limit is global)
"""

import asyncio
import logging
import random
import signal
import uuid

import asyncpg
import httpx

from app import kb, llm, rate_limit
from app.config import settings
from app.sender import send_reply

log = logging.getLogger("worker")

GEN_LEASE_S = 90  # > LLM timeout; refreshed after the rate-limit wait
SEND_LEASE_S = 60  # > send timeout
IDLE_POLL_S = 1.0

CLAIM_GEN_SQL = """
UPDATE tickets t
SET status = 'generating', lease_id = $1, lease_until = now() + make_interval(secs => $2),
    gen_attempts = gen_attempts + 1, updated_at = now()
WHERE t.id = (
    SELECT id FROM tickets
    WHERE (status = 'new' AND next_attempt_at <= now())
       OR (status = 'generating' AND lease_until < now())
    ORDER BY next_attempt_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *
"""

CLAIM_SEND_SQL = """
UPDATE tickets t
SET status = 'sending', lease_id = $1, lease_until = now() + make_interval(secs => $2),
    send_attempts = send_attempts + 1, updated_at = now()
WHERE t.id = (
    SELECT id FROM tickets
    WHERE (status = 'approved' AND next_attempt_at <= now())
       OR (status = 'sending' AND lease_until < now())
    ORDER BY next_attempt_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *
"""


def backoff_s(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    return min(cap, base * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)


# ---------------------------------------------------------------- generation


async def process_generation(pool: asyncpg.Pool, ticket) -> None:
    lease = ticket["lease_id"]
    tid = ticket["id"]
    found = kb.search(ticket["message"])
    articles = [a for a, _ in found]

    await rate_limit.acquire(pool, settings.llm_rate_limit_per_min)
    still_mine = await pool.fetchval(
        "UPDATE tickets SET lease_until = now() + make_interval(secs => $3) "
        "WHERE id = $1 AND lease_id = $2 AND status = 'generating' RETURNING id",
        tid, lease, float(GEN_LEASE_S),
    )
    if not still_mine:
        log.warning("gen: lost lease on ticket %s while waiting for rate limit", tid)
        return

    try:
        result = await llm.generate_draft(ticket["message"], articles)
    except llm.LLMError as e:
        attempts = ticket["gen_attempts"]
        if e.retryable and attempts < settings.gen_max_attempts:
            delay = backoff_s(attempts)
            log.warning("gen: ticket %s attempt %s failed (%s), retry in %.0fs", tid, attempts, e, delay)
            await pool.execute(
                "UPDATE tickets SET status = 'new', lease_id = NULL, lease_until = NULL, "
                "next_attempt_at = now() + make_interval(secs => $3), last_error = $4, updated_at = now() "
                "WHERE id = $1 AND lease_id = $2",
                tid, lease, delay, str(e),
            )
        else:
            log.error("gen: ticket %s failed permanently: %s", tid, e)
            await pool.execute(
                "UPDATE tickets SET status = 'gen_failed', lease_id = NULL, lease_until = NULL, "
                "last_error = $3, updated_at = now() WHERE id = $1 AND lease_id = $2",
                tid, lease, str(e),
            )
        return

    sources = [{"slug": a.slug, "title": a.title, "url": a.url} for a in articles]
    updated = await pool.fetchval(
        "UPDATE tickets SET status = 'draft_ready', draft = $3, draft_sources = $4::jsonb, llm_mode = $5, "
        "lease_id = NULL, lease_until = NULL, last_error = NULL, draft_ready_at = now(), updated_at = now() "
        "WHERE id = $1 AND lease_id = $2 RETURNING id",
        tid, lease, result.text, sources, result.mode,
    )
    if updated:
        log.info("gen: ticket %s draft ready (%s, %d sources)", tid, result.mode, len(sources))
    else:
        log.warning("gen: ticket %s result dropped, lease was taken over", tid)


# ------------------------------------------------------------------ delivery


async def process_send(pool: asyncpg.Pool, http: httpx.AsyncClient, ticket) -> None:
    lease = ticket["lease_id"]
    tid = ticket["id"]
    outcome = await send_reply(http, ticket)
    attempts = ticket["send_attempts"]

    if outcome.kind == "sent":
        await pool.execute(
            "UPDATE tickets SET status = 'sent', sent_at = now(), helpdesk_reply_id = $3, "
            "lease_id = NULL, lease_until = NULL, last_error = NULL, updated_at = now() "
            "WHERE id = $1 AND lease_id = $2",
            tid, lease, outcome.reply_id,
        )
        log.info("send: ticket %s delivered (%s, attempt %s)", tid, outcome.detail, attempts)
    elif outcome.kind == "retry" and attempts < settings.send_max_attempts:
        delay = backoff_s(attempts, base=3.0)
        log.warning("send: ticket %s attempt %s: %s, retry in %.0fs", tid, attempts, outcome.detail, delay)
        await pool.execute(
            "UPDATE tickets SET status = 'approved', lease_id = NULL, lease_until = NULL, "
            "next_attempt_at = now() + make_interval(secs => $3), last_error = $4, updated_at = now() "
            "WHERE id = $1 AND lease_id = $2",
            tid, lease, delay, outcome.detail,
        )
    else:
        log.error("send: ticket %s failed: %s", tid, outcome.detail)
        await pool.execute(
            "UPDATE tickets SET status = 'send_failed', lease_id = NULL, lease_until = NULL, "
            "last_error = $3, updated_at = now() WHERE id = $1 AND lease_id = $2",
            tid, lease, outcome.detail,
        )


# --------------------------------------------------------------------- loops


async def _claim_loop(pool, stop: asyncio.Event, concurrency: int, claim_sql: str, lease_s: int, handler, name: str):
    sem = asyncio.Semaphore(concurrency)
    tasks: set[asyncio.Task] = set()

    async def run(ticket):
        try:
            await handler(ticket)
        except Exception:
            # Lease will expire and the row will be picked up again.
            log.exception("%s: unexpected error on ticket %s", name, ticket["id"])
        finally:
            sem.release()

    try:
        while not stop.is_set():
            await sem.acquire()
            try:
                ticket = await pool.fetchrow(claim_sql, uuid.uuid4(), float(lease_s))
            except (asyncpg.PostgresError, OSError) as e:
                sem.release()
                log.warning("%s: claim failed: %r", name, e)
                await asyncio.sleep(2)
                continue
            if ticket is None:
                sem.release()
                try:
                    await asyncio.wait_for(stop.wait(), IDLE_POLL_S)
                except asyncio.TimeoutError:
                    pass
                continue
            t = asyncio.create_task(run(ticket))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_worker(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    kb.load()
    async with httpx.AsyncClient() as http:
        await asyncio.gather(
            _claim_loop(
                pool, stop, settings.llm_concurrency, CLAIM_GEN_SQL, GEN_LEASE_S,
                lambda t: process_generation(pool, t), "gen",
            ),
            _claim_loop(
                pool, stop, settings.send_concurrency, CLAIM_SEND_SQL, SEND_LEASE_S,
                lambda t: process_send(pool, http, t), "send",
            ),
        )


async def _main() -> None:
    from app.db import apply_schema, create_pool

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pool = await create_pool(size=settings.db_pool_size + 5)
    await apply_schema(pool)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info(
        "worker started: model=%s mode=%s llm_rate=%s/min concurrency=%s",
        settings.llm_model, "real" if settings.anthropic_api_key else "no-key(stub)",
        settings.llm_rate_limit_per_min, settings.llm_concurrency,
    )
    try:
        await run_worker(pool, stop)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
