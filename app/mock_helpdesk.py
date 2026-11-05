"""Helpdesk mock: accepts replies the way a flaky real helpdesk would.

POST /mock-helpdesk/tickets/{ticket_id}/replies
  * MOCK_FAIL_RATE  — share of 503 responses (nothing stored);
  * MOCK_SLOW_RATE  — share of requests where the reply IS stored but the response
                      comes after MOCK_SLOW_SECONDS (longer than our send timeout) —
                      the nasty "succeeded but the client thinks it failed" case;
  * otherwise 201 Created.
Requests with the same Idempotency-Key never create a second reply.

GET /mock-helpdesk/replies — what the "customers" actually received.
"""

import asyncio
import random

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/mock-helpdesk", tags=["helpdesk-mock"])
templates = Jinja2Templates(directory="app/templates")


class ReplyIn(BaseModel):
    ticket_id: str
    message: str


@router.post("/tickets/{ticket_id}/replies")
async def create_reply(
    ticket_id: str,
    body: ReplyIn,
    request: Request,
    idempotency_key: str | None = Header(default=None),
):
    if random.random() < settings.mock_fail_rate:
        return JSONResponse({"error": "temporarily unavailable"}, status_code=503)

    pool = request.app.state.pool
    if idempotency_key:
        row = await pool.fetchrow(
            """
            INSERT INTO mock_replies (idempotency_key, ticket_id, message) VALUES ($1, $2, $3)
            ON CONFLICT (idempotency_key) DO UPDATE SET attempts = mock_replies.attempts + 1
            RETURNING id, (xmax = 0) AS inserted
            """,
            idempotency_key, ticket_id, body.message,
        )
    else:
        row = await pool.fetchrow(
            "INSERT INTO mock_replies (ticket_id, message) VALUES ($1, $2) RETURNING id, true AS inserted",
            ticket_id, body.message,
        )

    if random.random() < settings.mock_slow_rate:
        await asyncio.sleep(settings.mock_slow_seconds)

    return JSONResponse(
        {"id": f"R-{row['id']}", "ticket_id": ticket_id, "replayed": not row["inserted"]},
        status_code=201,
    )


@router.get("/replies", response_class=HTMLResponse)
async def list_replies(request: Request, format: str = "html"):
    rows = await request.app.state.pool.fetch(
        "SELECT * FROM mock_replies ORDER BY id DESC LIMIT 500"
    )
    if format == "json":
        return JSONResponse(
            [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]
        )
    dup_tickets = await request.app.state.pool.fetchval(
        "SELECT count(*) FROM (SELECT ticket_id FROM mock_replies GROUP BY ticket_id HAVING count(*) > 1) x"
    )
    return templates.TemplateResponse(
        request, "mock_replies.html", {"rows": rows, "dup_tickets": dup_tickets}
    )
