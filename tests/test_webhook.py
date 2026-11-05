import asyncio
import time

from tests.conftest import event


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.text == "OK"


async def test_accepts_new_event(client, pool):
    r = await client.post("/webhooks/helpdesk", json=event())
    assert r.status_code == 202
    row = await pool.fetchrow("SELECT * FROM tickets")
    assert row["status"] == "new" and row["ticket_id"] == "T-1"


async def test_concurrent_duplicates_create_one_ticket(client, pool):
    rs = await asyncio.gather(*[client.post("/webhooks/helpdesk", json=event()) for _ in range(25)])
    codes = sorted(r.status_code for r in rs)
    assert codes.count(202) == 1 and codes.count(200) == 24
    assert await pool.fetchval("SELECT count(*) FROM tickets") == 1
    assert await pool.fetchval("SELECT received_count FROM tickets") == 25


async def test_burst_is_fast_and_lossless(client, pool):
    t0 = time.monotonic()
    rs = await asyncio.gather(*[client.post("/webhooks/helpdesk", json=event(i)) for i in range(200)])
    elapsed = time.monotonic() - t0
    assert all(r.status_code == 202 for r in rs)
    assert await pool.fetchval("SELECT count(*) FROM tickets") == 200
    assert elapsed < 10  # 200 requests total; each well under the 2 s budget


async def test_validation(client):
    r = await client.post("/webhooks/helpdesk", json=event(reply_url="not-a-url"))
    assert r.status_code == 422
    r = await client.post("/webhooks/helpdesk", json={"event_id": "x"})
    assert r.status_code == 422


async def test_secret(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "webhook_secret", "s3cret")
    assert (await client.post("/webhooks/helpdesk", json=event())).status_code == 401
    r = await client.post("/webhooks/helpdesk", json=event(), headers={"X-Webhook-Secret": "s3cret"})
    assert r.status_code == 202
