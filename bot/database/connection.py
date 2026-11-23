import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from bot.config import Settings


class Database:
    def __init__(self, pool):
        self.pool = pool
        self._guild_locks = defaultdict(asyncio.Lock)

    @classmethod
    async def connect(cls, url):
        # Disabling the statement cache also supports Neon pooled URLs, though direct is preferred.
        pool = await asyncpg.create_pool(
            url, min_size=1, max_size=5, statement_cache_size=0, command_timeout=60
        )
        return cls(pool)

    async def migrate(self):
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock(740193)")
                await con.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
                )
                for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                    if not await con.fetchval(
                        "SELECT 1 FROM schema_migrations WHERE name=$1", path.name
                    ):
                        await con.execute(path.read_text())
                        await con.execute(
                            "INSERT INTO schema_migrations(name) VALUES($1)", path.name
                        )

    @asynccontextmanager
    async def guild(self, guild_id):
        # Session lock spans committed journal writes AND remote role changes. This requires
        # a direct Postgres connection, not transaction-mode PgBouncer.
        # Role updates emitted during a draft must queue before acquiring a connection.
        # Otherwise waiting events can occupy the entire pool and block staff commands.
        async with self._guild_locks[guild_id]:
            async with self.pool.acquire() as con:
                await con.execute("SELECT pg_advisory_lock($1)", guild_id)
                try:
                    yield con
                finally:
                    await asyncio.shield(con.execute("SELECT pg_advisory_unlock($1)", guild_id))

    async def close(self):
        await self.pool.close()


async def migrate():
    db = await Database.connect(Settings.load(database_only=True).database_url)
    try:
        await db.migrate()
        print("PCL International: migrations complete.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(migrate())
