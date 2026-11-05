import asyncio
import time

from app import rate_limit


async def test_limit_is_enforced(pool):
    waits = [await rate_limit.try_acquire(pool, limit=5, window_s=2) for _ in range(7)]
    assert waits[:5] == [0.0] * 5
    assert all(w > 0 for w in waits[5:])
    assert await pool.fetchval("SELECT count(*) FROM llm_calls") == 5


async def test_concurrent_acquirers_never_exceed_limit(pool):
    # 20 concurrent callers, 10 per 1.5 s window: the second half must wait for the window to slide.
    t0 = time.monotonic()
    stamps = []

    async def one():
        await rate_limit.acquire(pool, limit=10, window_s=1.5)
        stamps.append(time.monotonic() - t0)

    await asyncio.gather(*[one() for _ in range(20)])
    assert len(stamps) == 20
    rows = await pool.fetch("SELECT at FROM llm_calls ORDER BY at")
    ats = [r["at"] for r in rows]
    assert len(ats) == 20
    # any 1.5 s window contains at most 10 requests
    for i in range(len(ats) - 10):
        assert (ats[i + 10] - ats[i]).total_seconds() >= 1.5
