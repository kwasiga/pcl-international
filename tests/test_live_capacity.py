import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.engine import Engine
from bot.services.rules import RuleError
from test_postgres import Guild, Role


class LiveCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.guild.role_map[30] = Role(30)
        self.member = self.guild.player(100, (1, 8))
        self.rows = []

        async def fetch(sql, *args):
            if "FROM cycle_exclusions" in sql:
                return [{"country_role_id": 1}]
            return [row for row in self.rows if row["user_id"] != args[1]]

        self.con = SimpleNamespace(
            fetch=AsyncMock(side_effect=fetch),
            fetchval=AsyncMock(return_value=1),
            fetchrow=AsyncMock(
                return_value={
                    "club_conflict": False,
                    "primary_club_role_id": 8,
                    "current_country_role_id": 1,
                }
            ),
            execute=AsyncMock(),
        )

        @asynccontextmanager
        async def guild_lock(gid):
            yield self.con

        self.engine = Engine(SimpleNamespace(guild=guild_lock), None)
        self.engine.config = AsyncMock(
            return_value={"club_limit": 2, "country_cap": 2, "international_gk_role_id": 30}
        )
        self.engine.role_ids = AsyncMock(
            side_effect=lambda con, gid, kind, enabled=False: (
                {1, 2, 3} if kind == "country" else {8, 9}
            )
        )
        self.engine.cycle = AsyncMock(return_value=1)
        self.engine.idle_player = AsyncMock()
        self.engine.refresh_player = AsyncMock(return_value=(8, False))

    async def test_counts_merge_roles_saved_reservations_and_failed_targets_once_per_player(self):
        self.guild.player(101, (1, 8))
        self.guild.player(104, (2, 9))  # Only Discord knows this player.
        self.guild.player(105, (2, 9)).bot = True
        self.rows = [
            {"user_id": 100, "country": 1, "club": 8},
            {"user_id": 101, "country": 1, "club": 8},
            {"user_id": 101, "country": 1, "club": 8},
            {"user_id": 102, "country": 2, "club": 9},  # Reserved place.
            {"user_id": 103, "country": 2, "club": None},  # Failed target.
            {"user_id": 106, "country": 1, "club": 8},  # Saved absent player.
        ]
        population, clubs = await self.engine.counts(
            self.con, self.guild.id, self.member.id, guild=self.guild
        )
        self.assertEqual(dict(population), {1: 2, 2: 3})
        self.assertEqual(dict(clubs), {(1, 8): 2, (2, 9): 2})

    async def test_untracked_country_role_holders_fill_the_cap_for_respin_proposals(self):
        self.guild.player(101, (2,))
        self.guild.player(102, (2,))
        self.assertEqual(await self.engine.respin_target(self.con, self.member), 3)

    async def test_gk_preference_cannot_select_a_full_country_without_a_gk(self):
        self.member.roles.append(self.guild.get_role(30))
        self.guild.player(101, (2,))
        self.guild.player(102, (2,))
        self.guild.player(103, (3, 30))
        self.engine.goalkeeper_counts = AsyncMock(return_value={3: 1})
        self.assertEqual(await self.engine.respin_target(self.con, self.member), 3)

    async def test_saved_and_live_same_player_do_not_make_a_country_look_full(self):
        self.guild.player(101, (2,))
        self.rows = [{"user_id": 101, "country": 2, "club": None}]
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            self.assertEqual(await self.engine.respin_target(self.con, self.member), 2)

    async def test_live_club_limit_still_blocks_a_country_below_total_cap(self):
        self.engine.config.return_value["country_cap"] = 15
        self.guild.player(101, (2, 8))
        self.guild.player(102, (2, 8))
        self.assertEqual(await self.engine.respin_target(self.con, self.member), 3)

    async def test_country_filling_after_proposal_is_rejected_before_journaling(self):
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            target = await self.engine.respin_target(self.con, self.member)
        self.assertEqual(target, 2)
        self.guild.player(101, (2,))
        self.guild.player(102, (2,))
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.prepare_operation(
                self.con, self.guild, self.member.id, target, "respin", 77
            )
        self.con.execute.assert_not_awaited()
        self.assertFalse(
            any(
                "INSERT INTO role_operations" in call.args[0]
                for call in self.con.fetchval.call_args_list
            )
        )
        self.assertEqual({role.id for role in self.member.roles}, {1, 8})

    async def test_manual_override_cannot_bypass_live_country_cap(self):
        self.guild.player(101, (2,))
        self.guild.player(102, (2,))
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.prepare_operation(
                self.con, self.guild, self.member.id, 2, "manual", 77, override=True
            )
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.needs_override(self.member, 2)

    async def test_no_full_country_appears_in_previous_country_exception_options(self):
        for uid, country in ((101, 2), (102, 2), (103, 3), (104, 3)):
            self.guild.player(uid, (country,))
        self.engine.respins_used = AsyncMock(return_value=0)
        _, target, exceptions, _, _ = await self.engine.propose_respin(self.member)
        self.assertIsNone(target)
        self.assertEqual(exceptions, [])

    async def test_member_list_is_loaded_before_counting_roles(self):
        self.guild.chunked = False

        async def chunk():
            self.guild.player(101, (2,))
            self.guild.player(102, (2,))
            self.guild.chunked = True

        self.guild.chunk = AsyncMock(side_effect=chunk)
        self.assertEqual(await self.engine.respin_target(self.con, self.member), 3)
        self.guild.chunk.assert_awaited_once()
