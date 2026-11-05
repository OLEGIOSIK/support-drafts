"""Operator UI (server-rendered HTML) + a small JSON API, and KB article pages."""

import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import kb
from app.config import settings

templates = Jinja2Templates(directory="app/templates")
basic = HTTPBasic(auto_error=False)

STATUSES = [
    "new", "generating", "draft_ready", "approved", "sending",
    "sent", "rejected", "gen_failed", "send_failed",
]


def require_operator(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
    if not settings.operator_password:
        return
    ok = creds is not None and hmac.compare_digest(creds.username, "operator") and hmac.compare_digest(
        creds.password, settings.operator_password
    )
    if not ok:
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})


router = APIRouter()
protected = APIRouter(dependencies=[Depends(require_operator)])


def _ticket_json(r) -> dict:
    d = dict(r)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif k in ("lease_id", "idempotency_key") and v is not None:
            d[k] = str(v)
    return d


# ------------------------------------------------------------------ HTML


@protected.get("/operator", response_class=HTMLResponse)
async def ticket_list(request: Request, status: str = "draft_ready"):
    pool = request.app.state.pool
    counts = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) AS n FROM tickets GROUP BY status")}
    if status == "all":
        rows = await pool.fetch("SELECT * FROM tickets ORDER BY id DESC LIMIT 200")
    else:
        rows = await pool.fetch("SELECT * FROM tickets WHERE status = $1 ORDER BY id LIMIT 200", status)
    return templates.TemplateResponse(
        request,
        "list.html",
        {"rows": rows, "counts": counts, "statuses": STATUSES, "current": status,
         "total": sum(counts.values())},
    )


@protected.get("/operator/tickets/{tid}", response_class=HTMLResponse)
async def ticket_detail(request: Request, tid: int, msg: str = ""):
    row = await request.app.state.pool.fetchrow("SELECT * FROM tickets WHERE id = $1", tid)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "ticket.html", {"t": row, "msg": msg})


def _back(tid: int, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/operator/tickets/{tid}?msg={msg}", status_code=303)


@protected.post("/operator/tickets/{tid}/approve")
async def approve(request: Request, tid: int, message: str = Form(...)):
    message = message.strip()
    if not message:
        return _back(tid, "empty")
    # Atomic state transition: a second click / second operator gets "conflict", not a second send.
    ok = await request.app.state.pool.fetchval(
        "UPDATE tickets SET status = 'approved', final_message = $2, approved_at = now(), "
        "next_attempt_at = now(), updated_at = now() WHERE id = $1 AND status = 'draft_ready' RETURNING id",
        tid, message,
    )
    return _back(tid, "approved" if ok else "conflict")


@protected.post("/operator/tickets/{tid}/reject")
async def reject(request: Request, tid: int):
    ok = await request.app.state.pool.fetchval(
        "UPDATE tickets SET status = 'rejected', updated_at = now() "
        "WHERE id = $1 AND status IN ('draft_ready', 'gen_failed') RETURNING id",
        tid,
    )
    return _back(tid, "rejected" if ok else "conflict")


@protected.post("/operator/tickets/{tid}/retry")
async def retry(request: Request, tid: int):
    ok = await request.app.state.pool.fetchval(
        """
        UPDATE tickets SET
            status = CASE status WHEN 'gen_failed' THEN 'new' ELSE 'approved' END,
            gen_attempts = CASE status WHEN 'gen_failed' THEN 0 ELSE gen_attempts END,
            send_attempts = CASE status WHEN 'send_failed' THEN 0 ELSE send_attempts END,
            next_attempt_at = now(), updated_at = now()
        WHERE id = $1 AND status IN ('gen_failed', 'send_failed') RETURNING id
        """,
        tid,
    )
    return _back(tid, "retried" if ok else "conflict")


@protected.post("/operator/tickets/{tid}/regenerate")
async def regenerate(request: Request, tid: int):
    ok = await request.app.state.pool.fetchval(
        "UPDATE tickets SET status = 'new', gen_attempts = 0, next_attempt_at = now(), updated_at = now() "
        "WHERE id = $1 AND status IN ('draft_ready', 'rejected') RETURNING id",
        tid,
    )
    return _back(tid, "regenerating" if ok else "conflict")


# ------------------------------------------------------------------ JSON API


@protected.get("/api/tickets")
async def api_list(request: Request, status: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 500))
    pool = request.app.state.pool
    if status:
        rows = await pool.fetch("SELECT * FROM tickets WHERE status = $1 ORDER BY id DESC LIMIT $2", status, limit)
    else:
        rows = await pool.fetch("SELECT * FROM tickets ORDER BY id DESC LIMIT $1", limit)
    return [_ticket_json(r) for r in rows]


@protected.get("/api/tickets/{tid}")
async def api_get(request: Request, tid: int):
    row = await request.app.state.pool.fetchrow("SELECT * FROM tickets WHERE id = $1", tid)
    if not row:
        raise HTTPException(404)
    return _ticket_json(row)


@protected.get("/api/stats")
async def api_stats(request: Request):
    pool = request.app.state.pool
    counts = {r["status"]: r["n"] for r in await pool.fetch("SELECT status, count(*) AS n FROM tickets GROUP BY status")}
    llm_last_min = await pool.fetchval("SELECT count(*) FROM llm_calls WHERE at > clock_timestamp() - interval '60 seconds'")
    return {"by_status": counts, "llm_requests_last_60s": llm_last_min}


# ------------------------------------------------------------------ KB (public)


@router.get("/kb", response_class=HTMLResponse)
async def kb_index(request: Request):
    return templates.TemplateResponse(request, "kb_index.html", {"articles": kb.all_articles()})


@router.get("/kb/{slug}", response_class=HTMLResponse)
async def kb_article(request: Request, slug: str):
    art = kb.get(slug)
    if not art:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "kb_article.html", {"a": art})


router.include_router(protected)
