import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands import StaffCommands
from bot.services.rules import RuleError
from bot.views.common import Pages
from test_postgres import Guild, Role


class CountryCountsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.guild.get_role(1).name = "Spain"
        self.guild.get_role(2).name = "Italy"

        @asynccontextmanager
        async def acquire():
            yield SimpleNamespace()

        self.engine = SimpleNamespace(
            config=AsyncMock(return_value={"country_cap": 2}),
            role_ids=AsyncMock(return_value={1, 2, 999}),
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
            user=SimpleNamespace(id=77),
            response=SimpleNamespace(defer=AsyncMock()),
        )

    async def run_command(self):
        await self.cog.country_counts.callback(self.cog, self.i)
        return self.cog.pages.call_args.args[2]

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_public_counts_use_current_roles_and_mark_full_and_over_cap(self, staff):
        staff.side_effect = RuleError("Staff only")
        self.guild.player(100, (1,))
        duplicate = self.guild.player(101, (1, 2))
        self.guild.members.append(duplicate)
        self.guild.player(102, (1,))
        self.guild.player(103, (2,))
        self.guild.player(104, (1,)).bot = True
        self.guild.player(105, (3, 9))  # Disabled country and unrelated club.
        self.assertEqual(
            await self.run_command(),
            ["<@&2> — **2/2 players** — **FULL**", "<@&1> — **3/2 players** — **OVER CAP**"],
        )
        kwargs = self.cog.pages.call_args.kwargs
        self.assertFalse(kwargs["staff_only"])
        self.assertEqual(kwargs["footer"], "Countries: 2 • Current players: 4")
        self.engine.config.assert_awaited_once_with(
            unittest.mock.ANY, self.guild.id, allow_reset=True, allow_paused=True
        )
        staff.assert_not_awaited()

    async def test_empty_countries_and_unlimited_cap_are_shown(self):
        self.engine.config.return_value = {"country_cap": None}
        self.guild.player(100, (1,))
        self.assertEqual(
            await self.run_command(),
            ["<@&2> — **0 players**", "<@&1> — **1 player**"],
        )

    async def test_report_has_eight_countries_per_page_and_keeps_alphabetical_order(self):
        self.engine.role_ids.return_value = set(range(100, 117))
        for rid in range(100, 117):
            self.guild.role_map[rid] = Role(rid)
            self.guild.get_role(rid).name = f"Country {rid}"
        lines = await self.run_command()
        kwargs = self.cog.pages.call_args.kwargs
        view = Pages(
            self.cog.db,
            77,
            "Country counts",
            lines,
            staff_only=kwargs["staff_only"],
            page_breaks=kwargs["page_breaks"],
        )
        self.assertEqual([page.count("<@&") for page in view.pages], [8, 8, 1])
        self.assertTrue(lines[0].startswith("<@&100>"))
        self.assertTrue(lines[-1].startswith("<@&116>"))

    async def test_incomplete_member_cache_is_loaded_before_counting(self):
        self.guild.chunked = False
        member = self.guild.player(100, (2,))
        self.guild.members.clear()
        self.guild.chunk = AsyncMock(return_value=[member])
        self.assertEqual((await self.run_command())[0], "<@&2> — **1/2 player**")
        self.guild.chunk.assert_awaited_once()

    async def test_missing_countries_gives_setup_instruction(self):
        self.engine.role_ids.return_value = set()
        self.assertEqual(
            await self.run_command(), ["No national teams configured. Add country roles in /setup."]
        )
        self.assertEqual(
            self.cog.pages.call_args.kwargs["footer"], "Countries: 0 • Current players: 0"
        )

    async def test_direct_messages_are_rejected(self):
        self.i.guild = None
        with self.assertRaisesRegex(RuleError, "inside your Discord server"):
            await self.run_command()
        self.cog.pages.assert_not_awaited()
