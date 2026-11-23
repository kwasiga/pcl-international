import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.engine import Engine
from bot.services.rules import RuleError
from test_postgres import Guild


@asynccontextmanager
async def context(value=None):
    yield value


class CountryReleaseTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, rows, roster):
        con = SimpleNamespace(
            transaction=context,
            fetch=AsyncMock(side_effect=[rows, roster]),
            execute=AsyncMock(),
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        engine.config = AsyncMock(return_value={"callups_channel_id": 30})
        engine.cycle = AsyncMock(return_value=10)
        engine.role_ids = AsyncMock(return_value={1, 2})
        engine.outbox = AsyncMock()
        engine.audit = AsyncMock()
        return engine, con

    def operation(self, country, user, oid=1):
        return {"id": oid, "batch_id": 5, "user_id": user, "target_country_id": country}

    async def test_random_release_publishes_only_one_complete_country(self):
        guild = Guild()
        guild.player(100, (1,))
        guild.player(101, (1,))
        guild.player(102, (2,))
        engine, con = self.make_engine(
            [self.operation(1, 100), self.operation(2, 102, 2)],
            [{"user_id": 100}, {"user_id": 101}],
        )
        with patch("bot.services.engine.secrets.choice", return_value=1) as choose:
            self.assertEqual(await engine.release_country(guild, 77), (1, 2))
        choose.assert_called_once_with([1, 2])
        engine.outbox.assert_awaited_once()
        payload = engine.outbox.call_args.args[4]
        self.assertIn("**Role 1 - <@&1>**", payload)
        self.assertIn("<@100>", payload)
        self.assertIn("<@101>", payload)
        self.assertNotIn("<@102>", payload)
        self.assertNotIn("<@&2>", payload)
        # Only the selected country's completed operations are marked released.
        self.assertEqual(con.execute.call_args.args[3], "batch-player:1")

    async def test_explicit_country_does_not_choose_randomly(self):
        guild = Guild()
        guild.player(102, (2,))
        engine, _ = self.make_engine(
            [self.operation(1, 100), self.operation(2, 102, 2)], [{"user_id": 102}]
        )
        with patch("bot.services.engine.secrets.choice") as choose:
            self.assertEqual(await engine.release_country(guild, 77, 2), (2, 1))
        choose.assert_not_called()

    async def test_exhausted_release_does_not_queue_messages(self):
        engine, _ = self.make_engine([], [])
        with self.assertRaisesRegex(RuleError, "No unreleased squads"):
            await engine.release_country(Guild(), 77)
        engine.outbox.assert_not_awaited()

    async def test_unready_country_does_not_fall_back_to_another(self):
        engine, _ = self.make_engine([self.operation(2, 100)], [])
        with self.assertRaisesRegex(RuleError, "That country has no unreleased"):
            await engine.release_country(Guild(), 77, 1)
        engine.outbox.assert_not_awaited()

    async def test_missing_country_role_is_not_selected(self):
        guild = Guild()
        del guild.role_map[1]
        engine, _ = self.make_engine([self.operation(1, 100)], [])
        with self.assertRaisesRegex(RuleError, "No unreleased squads"):
            await engine.release_country(guild, 77)
        engine.outbox.assert_not_awaited()

    async def test_role_discrepancy_stops_entire_squad_release(self):
        guild = Guild()
        guild.player(100, (1,))
        guild.player(101, ())
        engine, con = self.make_engine(
            [self.operation(1, 100)], [{"user_id": 100}, {"user_id": 101}]
        )
        with self.assertRaisesRegex(RuleError, "Run /sync"):
            await engine.release_country(guild, 77, 1)
        engine.outbox.assert_not_awaited()
        con.execute.assert_not_awaited()

    async def test_recovery_completes_batch_without_publishing(self):
        engine = Engine(None, None)
        engine.outbox = AsyncMock()
        con = SimpleNamespace(
            fetch=AsyncMock(return_value=[{"id": 5}]),
            fetchval=AsyncMock(return_value=None),
            execute=AsyncMock(),
        )
        await engine.finish_batches(con, Guild())
        con.execute.assert_awaited_once()
        self.assertIn("status='completed'", con.execute.call_args.args[0])
        engine.outbox.assert_not_awaited()
