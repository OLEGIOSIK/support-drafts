"""Inbound webhook from the helpdesk.

Contract: the helpdesk waits <= 2 s and re-delivers on timeout; the same event may
arrive many times, also concurrently. So the handler does only one thing — a single
idempotent INSERT ... ON CONFLICT (event_id) — and answers. All slow work (LLM) runs
in the worker, reading the tickets table as a queue.
"""

import asyncio
import hmac
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter()

# Leave headroom under the helpdesk's 2 s timeout: better an explicit 503 (=> retry)
# than an answer that arrives after the helpdesk already gave up.
HANDLER_BUDGET_S = 1.5


class HelpdeskEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    ticket_id: str = Field(min_length=1, max_length=200)
    customer_email: str = Field(min_length=3, max_length=320)
    message: str = Field(min_length=1, max_length=20_000)
    reply_url: str = Field(min_length=1, max_length=2_000)

    @field_validator("customer_email")
    @classmethod
    def _email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("invalid email")
        return v.strip()

    @field_validator("reply_url")
    @classmethod
    def _reply_url(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError("reply_url must be an absolute http(s) URL")
        allowed = settings.allowed_reply_hosts
        if allowed and p.hostname.lower() not in allowed and p.hostname.lower() not in settings.mock_hosts:
            raise ValueError("reply_url host is not allowed")
        return v


INSERT_SQL = """
INSERT INTO tickets (event_id, ticket_id, customer_email, message, reply_url)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (event_id) DO UPDATE
    SET received_count = tickets.received_count + 1
RETURNING id, (xmax = 0) AS inserted
"""


@router.post("/webhooks/helpdesk")
async def helpdesk_webhook(
    event: HelpdeskEvent,
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    if settings.webhook_secret and not hmac.compare_digest(
        x_webhook_secret or "", settings.webhook_secret
    ):
        raise HTTPException(status_code=401, detail="bad webhook secret")

    pool = request.app.state.pool
    try:
        row = await asyncio.wait_for(
            pool.fetchrow(
                INSERT_SQL,
                event.event_id,
                event.ticket_id,
                event.customer_email,
                event.message,
                event.reply_url,
            ),
            timeout=HANDLER_BUDGET_S,
        )
    except (asyncio.TimeoutError, OSError) as e:
        # Not stored (or unknown) => ask the helpdesk to retry. Retry is safe: dedup by event_id.
        log.warning("webhook: storage unavailable for %s: %r", event.event_id, e)
        return JSONResponse({"status": "retry"}, status_code=503)

    if row["inserted"]:
        log.info("webhook: accepted event=%s ticket=%s id=%s", event.event_id, event.ticket_id, row["id"])
        return JSONResponse({"status": "accepted", "id": row["id"]}, status_code=202)
    log.info("webhook: duplicate event=%s id=%s", event.event_id, row["id"])
    return JSONResponse({"status": "duplicate", "id": row["id"]}, status_code=200)
