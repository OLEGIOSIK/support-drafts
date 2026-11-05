import json
from pathlib import Path

import asyncpg

from app.config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_SCHEMA_LOCK_KEY = 7_420_001


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str | None = None, size: int | None = None) -> asyncpg.Pool:
    size = size or settings.db_pool_size
    return await asyncpg.create_pool(
        dsn or settings.database_url,
        min_size=1,
        max_size=size,
        init=_init_conn,
        command_timeout=30,
    )


async def apply_schema(pool: asyncpg.Pool) -> None:
    sql = SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        # app and worker start concurrently; CREATE ... IF NOT EXISTS is not race-safe on its own.
        await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
        try:
            await conn.execute(sql)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)
