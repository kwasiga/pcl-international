import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot.commands import StaffCommands
from bot.services.engine import Engine
from bot.services.roles import apply_roles
from bot.services.rules import RuleError
from test_postgres import Guild


class RemoveByIDTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.engine = SimpleNamespace(remove_country=AsyncMock(return_value=True))
        self.cog = StaffCommands(SimpleNamespace(engine=self.engine, db=None))
        self.cog.check = AsyncMock()
        self.i = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(id=77),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_departed_user_id_needs_no_member_resolution(self):
        await self.cog.remove_player.callback(self.cog, self.i, user_id="123456789012345678")
        self.engine.remove_country.assert_awaited_once_with(self.guild, 123456789012345678, 77)
        self.cog.check.assert_awaited_once_with(self.i)
        self.assertIn("History preserved", self.i.followup.send.call_args.args[0])

    async def test_existing_member_selector_is_still_supported(self):
        player = self.guild.player(100)
        await self.cog.remove_player.callback(self.cog, self.i, player)
        self.engine.remove_country.assert_awaited_once_with(self.guild, 100, 77)

    async def test_requires_one_target_and_rejects_invalid_ids(self):
        for kwargs in ({}, {"player": self.guild.player(100), "user_id": "100"}):
            with self.assertRaises(RuleError):
                await self.cog.remove_player.callback(self.cog, self.i, **kwargs)
        for value in ("name", "", "0", "-1", "12.3", "9" * 30, "9223372036854775808"):
            with self.assertRaises(RuleError):
                await self.cog.remove_player.callback(self.cog, self.i, user_id=value)
        self.engine.remove_country.assert_not_awaited()

    async def test_id_removal_remains_staff_only(self):
        self.cog.check.side_effect = RuleError("Admin role required")
        with self.assertRaisesRegex(RuleError, "Admin role required"):
            await self.cog.remove_player.callback(self.cog, self.i, user_id="100")
        self.engine.remove_country.assert_not_awaited()

    async def test_service_commits_removal_journal_before_execution(self):
        committed = False

        @asynccontextmanager
        async def transaction():
            nonlocal committed
            yield
            committed = True

        con = SimpleNamespace(transaction=transaction)

        @asynccontextmanager
        async def guild_lock(gid):
            yield con

        engine = Engine(SimpleNamespace(guild=guild_lock), None)
        engine.prepare_operation = AsyncMock(return_value=42)

        async def execute(*args):
            self.assertTrue(committed)
            return True

        engine.execute_operation = AsyncMock(side_effect=execute)
        self.assertTrue(await engine.remove_country(self.guild, 100, 77))
        engine.prepare_operation.assert_awaited_once_with(con, self.guild, 100, None, "remove", 77)

    async def test_discord_unknown_member_is_a_successful_role_removal(self):
        error = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10007, "message": "Unknown Member"},
        )
        guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=error))
        await apply_roles(guild, 100, {1, 2}, None, "PCL International: remove")
        guild.fetch_member.assert_awaited_once_with(100)
