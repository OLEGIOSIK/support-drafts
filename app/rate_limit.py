"""Global LLM rate limiter: N requests per sliding window, shared by every process.

State lives in Postgres (table llm_calls), serialized by a transaction-level advisory
lock, so the limit holds no matter how many workers/replicas run. Each *request* to
the provider takes a permit, including retries and the no-key mode request.
"""

import asyncio
import random

import asyncpg

_LOCK_KEY = 7_420_002

ACQUIRE_SQL = """
WITH cleanup AS (
    DELETE FROM llm_calls WHERE at < clock_timestamp() - make_interval(secs => $2::float8 * 5)
),
win AS (
    SELECT count(*) AS n, min(at) AS oldest
    FROM llm_calls WHERE at > clock_timestamp() - make_interval(secs => $2)
)
SELECT n,
       EXTRACT(EPOCH FROM (oldest + make_interval(secs => $2::float8) - clock_timestamp()))::float AS wait_s,
       n < $1 AS allowed
FROM win
"""


async def try_acquire(pool: asyncpg.Pool, limit: int, window_s: float = 60.0) -> float:
    """Take a permit if available. Returns 0 on success, otherwise seconds to wait."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _LOCK_KEY)
        row = await conn.fetchrow(ACQUIRE_SQL, limit, float(window_s))
        if row["allowed"]:
            await conn.execute("INSERT INTO llm_calls DEFAULT VALUES")
            return 0.0
        return max(row["wait_s"] or 0.0, 0.05)


async def acquire(pool: asyncpg.Pool, limit: int, window_s: float = 60.0) -> None:
    while True:
        wait = await try_acquire(pool, limit, window_s)
        if wait == 0.0:
            return
        # jitter so waiters don't stampede the lock at the same instant
        await asyncio.sleep(wait + random.uniform(0, 0.25))
