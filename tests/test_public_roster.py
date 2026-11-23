import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands import StaffCommands
from bot.services.rules import RuleError
from bot.views.common import Pages
from test_postgres import Guild, Role


class PublicRosterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.guild.get_role(1).name = "Ghana"
        self.con = SimpleNamespace(fetch=AsyncMock(return_value=[]))

        @asynccontextmanager
        async def acquire():
            yield self.con

        self.engine = SimpleNamespace(
            role_ids=AsyncMock(return_value={1}),
            config=AsyncMock(return_value={"club_limit": 5, "country_cap": 20}),
        )
        self.cog = StaffCommands(
            SimpleNamespace(
                engine=self.engine, db=SimpleNamespace(pool=SimpleNamespace(acquire=acquire))
            )
        )
        self.cog.pages = AsyncMock()
        self.i = SimpleNamespace(
            guild=self.guild,
            guild_id=self.guild.id,
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        )

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_regular_member_can_view_configured_country(self, staff):
        staff.side_effect = RuleError("Staff only")
        self.con.fetch.return_value = [
            {
                "user_id": 123,
                "primary_club_role_id": None,
                "club_conflict": False,
                "present": True,
            }
        ]
        await self.cog.roster.callback(self.cog, self.i, "1")
        staff.assert_not_awaited()
        self.i.response.defer.assert_awaited_once_with(ephemeral=True)
        self.engine.role_ids.assert_awaited_once_with(self.con, self.guild.id, "country", True)
        self.assertEqual(self.con.fetch.call_args.args[1:], (self.guild.id, 1))
        self.assertEqual(self.cog.pages.call_args.kwargs, {"staff_only": False})
        self.assertEqual(self.cog.pages.call_args.args[1], "Ghana — Active roster")
        self.assertIn("<@123>", self.cog.pages.call_args.args[2])

    async def test_unconfigured_or_disabled_roles_rejected_before_roster_query(self):
        for country in ("2", "8", "999999999999999999999999999999999999"):
            with self.subTest(country=country):
                with self.assertRaisesRegex(RuleError, "configured national team list"):
                    await self.cog.roster.callback(self.cog, self.i, country)
        self.con.fetch.assert_not_awaited()
        self.cog.pages.assert_not_awaited()

    async def test_stale_autocomplete_selection_is_revalidated(self):
        choices = await self.cog.roster_countries(self.i, "ghana")
        self.assertEqual([c.value for c in choices], ["1"])
        self.engine.role_ids.return_value = set()
        with self.assertRaisesRegex(RuleError, "configured national team list"):
            await self.cog.roster.callback(self.cog, self.i, choices[0].value)
        self.con.fetch.assert_not_awaited()

    async def test_deleted_country_role_is_rejected(self):
        del self.guild.role_map[1]
        with self.assertRaisesRegex(RuleError, "configured national team list"):
            await self.cog.roster.callback(self.cog, self.i, "1")
        self.con.fetch.assert_not_awaited()
        self.assertEqual(await self.cog.roster_countries(self.i, ""), [])

    async def test_arbitrary_typed_name_is_rejected(self):
        with self.assertRaisesRegex(RuleError, "configured national team list"):
            await self.cog.roster.callback(self.cog, self.i, "Club Role")
        self.con.fetch.assert_not_awaited()

    async def test_autocomplete_only_returns_this_guilds_configured_countries(self):
        choices = await self.cog.roster_countries(self.i, "gHa")
        self.assertEqual([(c.name, c.value) for c in choices], [("Ghana", "1")])
        self.engine.role_ids.assert_awaited_once_with(self.con, self.guild.id, "country", True)
        self.assertEqual(await self.cog.roster_countries(self.i, "Role 8"), [])

    async def test_autocomplete_limits_choices_and_allows_searching_remaining_countries(self):
        for rid in range(100, 130):
            self.guild.role_map[rid] = Role(rid)
        self.engine.role_ids.return_value = set(range(100, 130))
        self.assertEqual(len(await self.cog.roster_countries(self.i, "")), 25)
        choices = await self.cog.roster_countries(self.i, "Role 129")
        self.assertEqual([c.value for c in choices], ["129"])

    async def test_roster_cannot_be_used_in_dms(self):
        self.i.guild = None
        with self.assertRaisesRegex(RuleError, "inside your Discord server"):
            await self.cog.roster.callback(self.cog, self.i, "1")
        self.assertEqual(await self.cog.roster_countries(self.i, ""), [])
        self.con.fetch.assert_not_awaited()

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_roster_pagination_works_without_staff_role_but_remains_owner_only(self, staff):
        pages = Pages(None, 100, "Ghana", ["player"] * 100, staff_only=False)
        self.assertTrue(await pages.interaction_check(self.i))
        self.i.user.id = 101
        self.i.response.is_done = lambda: False
        self.assertFalse(await pages.interaction_check(self.i))
        self.i.response.send_message.assert_awaited_once()
        staff.assert_not_awaited()

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_other_paginated_commands_still_require_staff(self, staff):
        pages = Pages(None, 100, "History", ["private"])
        staff.side_effect = RuleError("Staff only")
        self.i.response.is_done = lambda: False
        self.assertFalse(await pages.interaction_check(self.i))
        staff.assert_awaited_once()
