import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.database.connection import Database


class SmallPool:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(2)
        self.connection = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def acquire(self):
        async with self.semaphore:
            yield self.connection


class DatabaseQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_guild_waiters_do_not_exhaust_pool(self):
        pool = SmallPool()
        db = Database(pool)

        async def update():
            async with db.guild(1):
                pass

        async def permission_lookup():
            async with pool.acquire():
                return "available"

        async with db.guild(1):
            waiters = [asyncio.create_task(update()) for _ in range(5)]
            try:
                await asyncio.sleep(0)
                self.assertEqual(
                    await asyncio.wait_for(permission_lookup(), timeout=1), "available"
                )
                self.assertTrue(all(not task.done() for task in waiters))
            finally:
                for task in waiters:
                    task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    async def test_different_guild_can_work_during_draft(self):
        db = Database(SmallPool())

        async def other_guild():
            async with db.guild(2):
                return "completed"

        async with db.guild(1):
            self.assertEqual(await asyncio.wait_for(other_guild(), timeout=1), "completed")

    async def test_errors_release_both_locks(self):
        pool = SmallPool()
        db = Database(pool)
        with self.assertRaisesRegex(ValueError, "failed"):
            async with db.guild(1):
                raise ValueError("failed")
        async with db.guild(1):
            pass
        statements = [call.args[0] for call in pool.connection.execute.call_args_list]
        self.assertEqual(statements.count("SELECT pg_advisory_lock($1)"), 2)
        self.assertEqual(statements.count("SELECT pg_advisory_unlock($1)"), 2)
