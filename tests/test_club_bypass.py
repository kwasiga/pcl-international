import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.commands import StaffCommands
from bot.services.engine import Engine
from bot.services.rules import RuleError, choose_country
from test_postgres import Guild


@asynccontextmanager
async def context(value=None):
    yield value


class BypassCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        for uid in range(100, 104):
            self.guild.player(uid, (1, 8))
        rows = [
            {"user_id": uid, "country": 1, "club": 8, "club_bypass": uid == 103}
            for uid in range(100, 104)
        ]
        self.con = SimpleNamespace(fetch=AsyncMock(return_value=rows))
        self.engine = Engine(None, None)
        self.engine.role_ids = AsyncMock(
            side_effect=lambda con, gid, kind, enabled=False: (
                {1, 2} if kind == "country" else {8, 9}
            )
        )

    async def test_exempt_player_still_uses_a_total_country_place_and_club_remains_full(self):
        population, clubs = await self.engine.counts(self.con, self.guild.id, guild=self.guild)
        self.assertEqual(dict(population), {1: 4})
        self.assertEqual(dict(clubs), {(1, 8): 3})
        self.assertIsNone(choose_country([1], population, clubs, 8, 3))
        self.assertIsNone(choose_country([1], population, clubs, None, 3, country_cap=4))
        self.assertEqual(choose_country([1], population, clubs, 9, 3, country_cap=5), 1)

    async def test_database_only_counts_apply_same_exemption(self):
        population, clubs = await self.engine.counts(self.con, self.guild.id)
        self.assertEqual(dict(population), {1: 4})
        self.assertEqual(dict(clubs), {(1, 8): 3})

    async def test_live_club_or_country_change_invalidates_old_bypass(self):
        exempt = self.guild.get_member(103)
        exempt.roles = [self.guild.get_role(1), self.guild.get_role(9)]
        population, clubs = await self.engine.counts(self.con, self.guild.id, guild=self.guild)
        self.assertEqual(population[1], 4)
        self.assertEqual(clubs[(1, 9)], 1)
        self.assertEqual(clubs[(1, 8)], 3)
        exempt.roles = [self.guild.get_role(2), self.guild.get_role(8)]
        _, clubs = await self.engine.counts(self.con, self.guild.id, guild=self.guild)
        self.assertEqual(clubs[(2, 8)], 1)


class BypassServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.player = self.guild.player(100, (1, 8))
        self.con = SimpleNamespace(
            transaction=context,
            execute=AsyncMock(),
            fetchval=AsyncMock(return_value=1),
            fetchrow=AsyncMock(return_value={"country_role_id": 1, "club_role_id": 8}),
        )
        self.engine = Engine(SimpleNamespace(guild=lambda _: context(self.con)), None)
        self.engine.config = AsyncMock()
        self.engine.idle_player = AsyncMock()
        self.engine.check_not_banned = AsyncMock()
        self.engine.refresh_player = AsyncMock(return_value=(8, False))
        self.engine.cycle = AsyncMock(return_value=7)
        self.engine.audit = AsyncMock()

    async def test_grant_is_scoped_to_existing_country_club_and_cycle_with_audit(self):
        self.assertEqual(await self.engine.set_club_bypass(self.player, 77), (1, 8))
        self.assertEqual(
            self.con.execute.call_args.args[1:],
            (self.guild.id, 100, 7, 1, 8, 77, "Club changed after draft"),
        )
        self.engine.audit.assert_awaited_once_with(
            self.con,
            self.guild.id,
            "club-limit bypass enabled",
            77,
            100,
            country=1,
            club=8,
            reason="Club changed after draft",
        )
        self.assertEqual({role.id for role in self.player.roles}, {1, 8})

    async def test_grant_requires_current_country_club_and_resolved_primary(self):
        self.con.fetchval.return_value = None
        with self.assertRaisesRegex(RuleError, "saved country"):
            await self.engine.set_club_bypass(self.player, 77)
        self.con.fetchval.return_value = 2  # Player does not hold country 2.
        with self.assertRaisesRegex(RuleError, "saved country"):
            await self.engine.set_club_bypass(self.player, 77)
        self.con.fetchval.return_value = 1
        self.engine.refresh_player.return_value = (None, False)
        with self.assertRaisesRegex(RuleError, "enabled league club"):
            await self.engine.set_club_bypass(self.player, 77)
        self.engine.refresh_player.return_value = (None, True)
        with self.assertRaisesRegex(RuleError, "primary club"):
            await self.engine.set_club_bypass(self.player, 77)
        self.con.execute.assert_not_awaited()

    async def test_revoke_needs_no_current_club_and_does_not_reassign_player(self):
        self.assertEqual(await self.engine.set_club_bypass(self.player, 77, False), (1, 8))
        self.engine.refresh_player.assert_not_awaited()
        self.assertIn("DELETE FROM club_limit_bypasses", self.con.fetchrow.call_args.args[0])
        self.assertEqual({role.id for role in self.player.roles}, {1, 8})


class BypassCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_is_staff_only_and_supports_removing_bypass(self):
        engine = SimpleNamespace(set_club_bypass=AsyncMock(return_value=(1, 8)))
        cog = StaffCommands(SimpleNamespace(engine=engine, db=None))
        cog.check = AsyncMock(side_effect=RuleError("Admin role required"))
        player = SimpleNamespace(mention="<@100>")
        i = SimpleNamespace(user=SimpleNamespace(id=77), followup=SimpleNamespace(send=AsyncMock()))
        with self.assertRaisesRegex(RuleError, "Admin role required"):
            await cog.bypass.callback(cog, i, player)
        engine.set_club_bypass.assert_not_awaited()
        cog.check.side_effect = None
        await cog.bypass.callback(cog, i, player, False)
        engine.set_club_bypass.assert_awaited_once_with(
            player, 77, False, "Club changed after draft"
        )
        self.assertIn("bypass removed", i.followup.send.call_args.args[0])
