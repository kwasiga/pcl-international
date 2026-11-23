import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands import StaffCommands
from bot.services.rules import RuleError
from bot.views.common import Pages


class PublicHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.con = SimpleNamespace(fetch=AsyncMock(return_value=[]))

        @asynccontextmanager
        async def acquire():
            yield self.con

        self.cog = StaffCommands(
            SimpleNamespace(
                engine=SimpleNamespace(
                    cycle=AsyncMock(return_value=10), respins_used=AsyncMock(return_value=0)
                ),
                db=SimpleNamespace(pool=SimpleNamespace(acquire=acquire)),
            )
        )
        self.cog.pages = AsyncMock()
        self.i = SimpleNamespace(
            guild=SimpleNamespace(id=50001),
            guild_id=50001,
            user=SimpleNamespace(id=100, display_name="Player"),
            response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False
            ),
        )

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_history_defaults_to_invoking_player_without_staff_access(self, staff):
        staff.side_effect = RuleError("Staff only")
        self.cog.engine.respins_used.return_value = 1
        self.con.fetch.return_value = [
            {
                "from_country_id": 1,
                "to_country_id": 2,
                "actor_id": 77,
                "created_at": datetime(2026, 11, 23, tzinfo=timezone.utc),
                "cycle_id": 10,
                "action": "respin",
                "override_type": "respin_limit",
            }
        ]
        await self.cog.history.callback(self.cog, self.i)
        staff.assert_not_awaited()
        self.i.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(self.con.fetch.call_args.args[1:], (50001, 100))
        self.assertEqual(
            self.cog.pages.call_args.kwargs,
            {"staff_only": False, "footer": "Respins remaining: 2/3"},
        )
        self.cog.engine.respins_used.assert_awaited_once_with(self.con, 50001, 100, 10)
        text = "\n".join(self.cog.pages.call_args.args[2])
        self.assertIn("<@&1> → <@&2>", text)
        self.assertIn("reroll limit override", text)

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_player_can_explicitly_select_themselves(self, staff):
        own_member = SimpleNamespace(id=100, display_name="Player")
        await self.cog.history.callback(self.cog, self.i, own_member)
        staff.assert_not_awaited()
        self.assertEqual(self.con.fetch.call_args.args[1:], (50001, 100))

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_regular_player_cannot_read_another_players_history(self, staff):
        staff.side_effect = RuleError("Admin role required")
        other = SimpleNamespace(id=101, display_name="Other player")
        with self.assertRaisesRegex(RuleError, "Admin role required"):
            await self.cog.history.callback(self.cog, self.i, other)
        self.con.fetch.assert_not_awaited()
        self.cog.pages.assert_not_awaited()

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_staff_can_still_look_up_another_player(self, staff):
        other = SimpleNamespace(id=101, display_name="Other player")
        await self.cog.history.callback(self.cog, self.i, other)
        staff.assert_awaited_once_with(self.cog.db, self.i.user)
        self.assertEqual(self.con.fetch.call_args.args[1:], (50001, 101))
        self.assertEqual(
            self.cog.pages.call_args.kwargs,
            {"staff_only": True, "footer": "Respins remaining: 3/3"},
        )
        self.cog.engine.respins_used.assert_awaited_once_with(self.con, 50001, 101, 10)

    async def test_history_is_server_only(self):
        self.i.guild = None
        with self.assertRaisesRegex(RuleError, "inside your Discord server"):
            await self.cog.history.callback(self.cog, self.i)
        self.con.fetch.assert_not_awaited()

    async def test_empty_self_history_is_supported(self):
        await self.cog.history.callback(self.cog, self.i)
        self.assertEqual(self.cog.pages.call_args.args[2], [])
        view = Pages(None, 100, "Player — History", [], **self.cog.pages.call_args.kwargs)
        self.assertIn("No records.", view.embed().description)
        self.assertIn("Respins remaining: 3/3", view.embed().footer.text)

    async def test_remaining_respins_are_clamped_at_zero_even_with_admin_overrides(self):
        for used, remaining in ((0, 3), (1, 2), (2, 1), (3, 0), (4, 0)):
            with self.subTest(used=used):
                self.cog.engine.respins_used.return_value = used
                await self.cog.history.callback(self.cog, self.i)
                self.assertEqual(
                    self.cog.pages.call_args.kwargs["footer"], f"Respins remaining: {remaining}/3"
                )

    async def test_remaining_count_is_shown_on_every_history_page(self):
        view = Pages(
            None,
            100,
            "Player — History",
            ["transfer " * 100] * 20,
            staff_only=False,
            footer="Respins remaining: 1/3",
        )
        self.assertGreater(len(view.pages), 1)
        for index in range(len(view.pages)):
            view.index = index
            footer = view.embed().footer.text
            self.assertIn(f"Page {index + 1}/{len(view.pages)}", footer)
            self.assertTrue(footer.endswith("Respins remaining: 1/3"))

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_owner_can_navigate_history_without_staff_role(self, staff):
        staff.side_effect = RuleError("Staff only")
        view = Pages(None, 100, "Player — History", ["transfer"] * 100, staff_only=False)
        self.assertTrue(await view.interaction_check(self.i))
        self.i.user = SimpleNamespace(id=101)
        self.assertFalse(await view.interaction_check(self.i))
        staff.assert_not_awaited()
