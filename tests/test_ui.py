import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.commands import StaffCommands
from bot.config import Settings
from bot.main import PCLInternational
from bot.services.rules import RuleError
from bot.views.common import Confirmation
from bot.views.setup import SetupPanel


def interaction(uid=10):
    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        response=SimpleNamespace(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=lambda: False,
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class UITests(unittest.IsolatedAsyncioTestCase):
    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_cancel_never_executes_respin_or_changes_history(self, staff):
        action = AsyncMock()
        panel = Confirmation(MagicMock(), 10, action)
        await panel.children[1].callback(interaction())
        action.assert_not_awaited()
        self.assertTrue(panel.claimed)

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_confirm_executes_once(self, staff):
        action = AsyncMock(return_value="done")
        panel = Confirmation(MagicMock(), 10, action)
        i = interaction()
        await panel.children[0].callback(i)
        await panel.children[0].callback(i)
        action.assert_awaited_once()
        staff.assert_awaited_once()

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_permissions_rechecked_when_clicked(self, staff):
        panel = Confirmation(MagicMock(), 10, AsyncMock())
        staff.side_effect = RuleError("role removed")
        self.assertFalse(await panel.interaction_check(interaction()))
        panel.action.assert_not_awaited()

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_another_staff_member_cannot_use_panel(self, staff):
        panel = Confirmation(MagicMock(), 10, AsyncMock())
        self.assertFalse(await panel.interaction_check(interaction(11)))
        panel.action.assert_not_awaited()
        staff.assert_not_awaited()

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_long_draft_shows_live_progress_then_final_result(self, staff):
        updated, finish = asyncio.Event(), asyncio.Event()
        i = interaction()

        async def edit(**kwargs):
            if "2/5 players" in kwargs["content"]:
                updated.set()

        async def action(click, report):
            await report("Assigning: **2/5 players**")
            await finish.wait()
            return "Draft finished: 5 successful."

        i.edit_original_response.side_effect = edit
        panel = Confirmation(MagicMock(), 10, action, progress=True)
        task = asyncio.create_task(panel.children[0].callback(i))
        try:
            await asyncio.wait_for(updated.wait(), timeout=1)
            self.assertFalse(task.done())
        finally:
            finish.set()
            await task
        self.assertEqual(
            i.edit_original_response.call_args.kwargs["content"], "Draft finished: 5 successful."
        )
        self.assertIsNone(i.edit_original_response.call_args.kwargs["view"])

    @patch("bot.views.common.require_staff", new_callable=AsyncMock)
    async def test_confirmation_error_replaces_working_message(self, staff):
        panel = Confirmation(
            MagicMock(), 10, AsyncMock(side_effect=RuleError("No eligible players"))
        )
        i = interaction()
        i.response.is_done = lambda: True
        with self.assertRaises(RuleError) as raised:
            await panel.children[0].callback(i)
        await panel.on_error(i, raised.exception, panel.children[0])
        self.assertEqual(
            i.edit_original_response.call_args.kwargs,
            {"content": "No eligible players", "view": None, "embed": None},
        )
        i.followup.send.assert_not_awaited()

    async def test_command_acknowledges_before_permission_lookup(self):
        i = interaction()

        async def check_staff(*args, **kwargs):
            i.response.defer.assert_awaited_once_with(ephemeral=True)

        bot = SimpleNamespace(engine=MagicMock(), db=MagicMock())
        with patch("bot.commands.require_staff", side_effect=check_staff):
            await StaffCommands(bot).check(i)

    async def test_setup_sections_fit_discord_component_limits(self):
        panel = SetupPanel(MagicMock(), 10, MagicMock(), None, set(), set())
        for section in (
            "overview",
            "staff",
            "countries",
            "clubs",
            "channels",
            "goalkeepers",
        ):
            panel.section = section
            panel.build()
            components = panel.to_components()
            self.assertLessEqual(len(components), 5)
            for row in components:
                self.assertLessEqual(len(row["components"]), 5)
        self.assertIn("PCL International", panel.embed().title)

    async def test_country_selection_supports_incremental_batches_over_25(self):
        panel = SetupPanel(MagicMock(), 10, MagicMock(), None, set(range(1, 26)), set())
        panel.values["countries"] |= set(range(26, 51))
        panel.section = "countries"
        panel.build()
        self.assertEqual(len(panel.values["countries"]), 50)
        self.assertEqual(panel.children[1].max_values, 25)

    async def test_slash_command_signatures_serialize(self):
        bot = PCLInternational(Settings("unused", "unused"), MagicMock())
        try:
            await bot.add_cog(StaffCommands(bot))
            names = {c.name for c in bot.tree.get_commands()}
            self.assertEqual(
                names,
                {
                    "setup",
                    "waitlist",
                    "club",
                    "draft",
                    "assign",
                    "remove",
                    "respin",
                    "roster",
                    "goalkeepers",
                    "history",
                    "status",
                    "sync",
                    "reset",
                    "clear",
                },
            )
            for command in bot.tree.get_commands():
                data = command.to_dict(bot.tree)
                self.assertTrue(data["description"])
                if command.name == "draft":
                    self.assertEqual({o["name"] for o in data["options"]}, {"run", "country"})
                    reveal = next(o for o in data["options"] if o["name"] == "country")
                    self.assertFalse(reveal["options"][0]["required"])
                elif command.name == "roster":
                    self.assertEqual(data["options"][0]["type"], 3)
                    self.assertTrue(data["options"][0]["autocomplete"])
                elif command.name == "respin":
                    override = next(o for o in data["options"] if o["name"] == "override_limit")
                    self.assertEqual(override["type"], 5)
                    self.assertFalse(override["required"])
                elif command.name == "history":
                    self.assertEqual(data["options"][0]["name"], "player")
                    self.assertFalse(data["options"][0]["required"])
        finally:
            await bot.close()
