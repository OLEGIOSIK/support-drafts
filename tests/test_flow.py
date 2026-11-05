"""End-to-end-ish: webhook -> generation -> operator approve -> delivery, with failures."""

import asyncio
import uuid

import httpx
import respx

from app import llm, worker
from app.sender import resolve_reply_url
from tests.conftest import event


async def _claim(pool, sql):
    return await pool.fetchrow(sql, uuid.uuid4(), 60.0)


async def _generate(pool):
    t = await _claim(pool, worker.CLAIM_GEN_SQL)
    await worker.process_generation(pool, t)
    return await pool.fetchrow("SELECT * FROM tickets WHERE id = $1", t["id"])


def fake_provider(monkeypatch, api_key: str, responses: list):
    """Real AsyncAnthropic client whose transport is a local mock (SDK 2.x uses httpx2)."""
    import anthropic
    import httpx2

    seen = []

    def handler(request: httpx2.Request):
        seen.append(request)
        status, body = responses[min(len(seen), len(responses)) - 1]
        return httpx2.Response(status, json=body)

    c = anthropic.AsyncAnthropic(
        api_key=api_key or llm.NO_KEY_PLACEHOLDER,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    monkeypatch.setattr(llm, "_client", c)
    return seen


ERR_401 = (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
ERR_529 = (529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
OK_200 = (200, {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "Вітаємо! Відкрийте звіт → Поділитися → Експорт → PDF."}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 10},
})


async def test_no_key_mode_makes_real_request_and_uses_stub(client, pool, fast_settings, monkeypatch):
    seen = fake_provider(monkeypatch, "", [ERR_401])
    await client.post("/webhooks/helpdesk", json=event())
    t = await _generate(pool)
    assert len(seen) == 1 and seen[0].url.path == "/v1/messages"
    assert llm.NO_KEY_PLACEHOLDER in str(dict(seen[0].headers))  # sent with the placeholder credential
    assert t["status"] == "draft_ready" and t["llm_mode"] == "stub"
    assert "/kb/export-pdf" in t["draft"]
    assert t["draft_sources"][0]["slug"] == "export-pdf"
    assert await pool.fetchval("SELECT count(*) FROM llm_calls") == 1  # counted by the limiter


async def test_no_key_mode_keeps_realistic_latency(monkeypatch, fast_settings):
    import time

    from app.config import settings

    monkeypatch.setattr(settings, "stub_delay_min_s", 0.3)
    monkeypatch.setattr(settings, "stub_delay_max_s", 0.4)
    fake_provider(monkeypatch, "", [ERR_401])
    t0 = time.monotonic()
    r = await llm.generate_draft("pdf", [])
    assert 0.3 <= time.monotonic() - t0 < 0.6 and r.mode == "stub"


async def test_real_mode_retries_transient_errors(client, pool, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    seen = fake_provider(monkeypatch, "sk-test", [ERR_529, OK_200])
    await client.post("/webhooks/helpdesk", json=event())

    t = await _generate(pool)
    assert t["status"] == "new" and t["last_error"] == "provider 529"  # scheduled for retry

    await pool.execute("UPDATE tickets SET next_attempt_at = now()")
    t = await _generate(pool)
    assert t["status"] == "draft_ready" and t["llm_mode"] == "real" and t["gen_attempts"] == 2
    assert "Експорт" in t["draft"]
    body = seen[-1].read().decode()
    assert '"fallbacks":"default"' in body.replace(" ", "") and "export-pdf" in body


async def test_real_mode_auth_error_is_permanent(client, pool, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-bad")
    fake_provider(monkeypatch, "sk-bad", [ERR_401])
    await client.post("/webhooks/helpdesk", json=event())
    t = await _generate(pool)
    assert t["status"] == "gen_failed" and "401" in t["last_error"]


async def test_approve_is_atomic(client, pool):
    await client.post("/webhooks/helpdesk", json=event())
    await pool.execute("UPDATE tickets SET status = 'draft_ready', draft = 'x'")
    rs = await asyncio.gather(*[
        client.post("/operator/tickets/1/approve", data={"message": "Відповідь"}) for _ in range(5)
    ])
    locations = [r.headers["location"] for r in rs]
    assert sum("msg=approved" in loc for loc in locations) == 1
    assert sum("msg=conflict" in loc for loc in locations) == 4


async def test_reject(client, pool):
    await client.post("/webhooks/helpdesk", json=event())
    await pool.execute("UPDATE tickets SET status = 'draft_ready', draft = 'x'")
    r = await client.post("/operator/tickets/1/reject")
    assert "msg=rejected" in r.headers["location"]
    assert await pool.fetchval("SELECT status FROM tickets") == "rejected"
    # a rejected ticket can't be approved
    r = await client.post("/operator/tickets/1/approve", data={"message": "x"})
    assert "msg=conflict" in r.headers["location"]


async def _approved_ticket(client, pool, i=1):
    await client.post("/webhooks/helpdesk", json=event(i))
    await pool.execute(
        "UPDATE tickets SET status = 'approved', final_message = 'Відповідь', next_attempt_at = now() WHERE id = $1", i
    )


async def test_delivery_retries_5xx_and_timeout_with_same_idempotency_key(client, pool):
    await _approved_ticket(client, pool)
    key = str(await pool.fetchval("SELECT idempotency_key FROM tickets"))
    url = resolve_reply_url("https://helpdesk.example.com/tickets/T-1/replies")
    seen_keys = []

    def handler(request):
        seen_keys.append(request.headers["idempotency-key"])
        n = len(seen_keys)
        if n == 1:
            return httpx.Response(503)
        if n == 2:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(201, json={"id": "R-1"})

    async with httpx.AsyncClient() as http:
        with respx.mock() as mock:
            mock.post(url).mock(side_effect=handler)
            for _ in range(3):
                t = await _claim(pool, worker.CLAIM_SEND_SQL)
                await worker.process_send(pool, http, t)
                await pool.execute("UPDATE tickets SET next_attempt_at = now()")

    row = await pool.fetchrow("SELECT * FROM tickets")
    assert row["status"] == "sent" and row["send_attempts"] == 3 and row["helpdesk_reply_id"] == "R-1"
    assert seen_keys == [key, key, key]


async def test_permanent_4xx_goes_to_send_failed(client, pool):
    await _approved_ticket(client, pool)
    url = resolve_reply_url("https://helpdesk.example.com/tickets/T-1/replies")
    async with httpx.AsyncClient() as http:
        with respx.mock() as mock:
            mock.post(url).respond(404)
            t = await _claim(pool, worker.CLAIM_SEND_SQL)
            await worker.process_send(pool, http, t)
    assert await pool.fetchval("SELECT status FROM tickets") == "send_failed"


async def test_mock_helpdesk_dedups_by_idempotency_key(client, pool, fast_settings):
    body = {"ticket_id": "T-1", "message": "hi"}
    h = {"Idempotency-Key": "k1"}
    rs = await asyncio.gather(*[client.post("/mock-helpdesk/tickets/T-1/replies", json=body, headers=h) for _ in range(5)])
    assert all(r.status_code == 201 for r in rs)
    assert len({r.json()["id"] for r in rs}) == 1
    assert await pool.fetchval("SELECT count(*) FROM mock_replies") == 1
    assert await pool.fetchval("SELECT attempts FROM mock_replies") == 5


async def test_expired_lease_is_reclaimed(client, pool):
    await client.post("/webhooks/helpdesk", json=event())
    first = await _claim(pool, worker.CLAIM_GEN_SQL)
    assert await _claim(pool, worker.CLAIM_GEN_SQL) is None  # leased
    await pool.execute("UPDATE tickets SET lease_until = now() - interval '1 second'")  # worker "died"
    second = await _claim(pool, worker.CLAIM_GEN_SQL)
    assert second["id"] == first["id"] and second["lease_id"] != first["lease_id"]
    # the stale worker can't overwrite the new owner's result
    stale = await pool.fetchval(
        "UPDATE tickets SET status = 'draft_ready' WHERE id = $1 AND lease_id = $2 RETURNING id",
        first["id"], first["lease_id"],
    )
    assert stale is None
