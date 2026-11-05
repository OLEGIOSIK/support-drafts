import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse

from app import kb
from app.config import settings
from app.db import apply_schema, create_pool
from app.mock_helpdesk import router as mock_router
from app.operator import router as operator_router
from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    await apply_schema(app.state.pool)
    kb.load()

    worker_task = None
    stop = asyncio.Event()
    if settings.run_worker:
        from app.worker import run_worker

        log.info("starting embedded worker")
        worker_task = asyncio.create_task(run_worker(app.state.pool, stop))
    try:
        yield
    finally:
        stop.set()
        if worker_task:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
        await app.state.pool.close()


app = FastAPI(title="Support drafts", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(operator_router)
app.include_router(mock_router)


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/operator")
