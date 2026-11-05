"""Delivery of approved replies to the helpdesk (POST <reply_url>).

"The customer must not receive the same reply twice" while the helpdesk sometimes
returns 5xx or does not answer in time. A timeout is ambiguous — the reply may have
been created — so we retry, but every attempt for a ticket carries the same
`Idempotency-Key` (generated once per ticket), and the helpdesk dedups by it.
Locally, a ticket is sent by at most one worker at a time (lease) and only after an
atomic draft_ready -> approved transition, so double clicks don't create extra sends.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    kind: str  # "sent" | "retry" | "failed"
    detail: str = ""
    reply_id: str | None = None


def resolve_reply_url(reply_url: str) -> str:
    """Route reply_url hosts listed in MOCK_HELPDESK_HOSTS to the built-in mock."""
    p = urlparse(reply_url)
    if (p.hostname or "").lower() in settings.mock_hosts:
        path = p.path + (f"?{p.query}" if p.query else "")
        return f"{settings.internal_url}/mock-helpdesk{path}"
    return reply_url


async def send_reply(http: httpx.AsyncClient, ticket) -> SendOutcome:
    url = resolve_reply_url(ticket["reply_url"])
    payload = {"ticket_id": ticket["ticket_id"], "message": ticket["final_message"]}
    headers = {"Idempotency-Key": str(ticket["idempotency_key"])}
    try:
        resp = await http.post(url, json=payload, headers=headers, timeout=settings.send_timeout_s)
    except httpx.TimeoutException:
        return SendOutcome("retry", "helpdesk timeout")
    except httpx.TransportError as e:
        return SendOutcome("retry", f"transport error: {e!r}")

    if resp.status_code in (200, 201):
        reply_id = None
        try:
            reply_id = str(resp.json().get("id"))
        except Exception:
            pass
        return SendOutcome("sent", f"helpdesk {resp.status_code}", reply_id)
    if resp.status_code == 409:
        # Idempotency conflict: the reply with this key already exists => delivered.
        return SendOutcome("sent", "helpdesk 409 (already delivered)")
    if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
        return SendOutcome("retry", f"helpdesk {resp.status_code}")
    return SendOutcome("failed", f"helpdesk {resp.status_code}: {resp.text[:300]}")
