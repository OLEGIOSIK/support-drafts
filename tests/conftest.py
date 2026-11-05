import os

# Must be set before app modules read settings.
os.environ.setdefault("DATABASE_URL", "postgresql://support:support@localhost:5432/support_test")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["RUN_WORKER"] = "false"
os.environ["OPERATOR_PASSWORD"] = ""
os.environ["WEBHOOK_SECRET"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import kb  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import apply_schema, create_pool  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
async def pool():
    if os.environ["DATABASE_URL"].rstrip("/").endswith("/support"):
        raise RuntimeError("refusing to run tests against the main database")
    p = await create_pool(os.environ["DATABASE_URL"], size=20)
    await apply_schema(p)
    await p.execute("TRUNCATE tickets, llm_calls, mock_replies RESTART IDENTITY")
    kb.load()
    yield p
    await p.close()


@pytest.fixture
async def client(pool):
    app.state.pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def fast_settings(monkeypatch):
    monkeypatch.setattr(settings, "stub_delay_min_s", 0.0)
    monkeypatch.setattr(settings, "stub_delay_max_s", 0.0)
    monkeypatch.setattr(settings, "mock_fail_rate", 0.0)
    monkeypatch.setattr(settings, "mock_slow_rate", 0.0)
    return settings


def event(i: int = 1, **kw) -> dict:
    e = {
        "event_id": f"evt_{i}",
        "ticket_id": f"T-{i}",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": f"https://helpdesk.example.com/tickets/T-{i}/replies",
    }
    e.update(kw)
    return e
