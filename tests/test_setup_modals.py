import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.services.rules import RuleError
from bot.views.setup import ClubLimitModal, CountryCapModal, SetupPanel
from test_postgres import Guild


def modal_interaction(uid=10, message=None):
    state = {"acknowledged": False}

    async def defer(**kwargs):
        state["acknowledged"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=uid),
        message=message,
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=defer), is_done=lambda: state["acknowledged"]
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class SetupModalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = MagicMock()
        engine.save_config = AsyncMock()
        self.panel = SetupPanel(engine, 10, Guild(), None, {1, 2}, {8})
        self.panel.message = SimpleNamespace(edit=AsyncMock())

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_country_cap_17_acknowledges_before_database_and_updates_parent_panel(
        self, staff
    ):
        i = modal_interaction()

        async def permissions(*args, **kwargs):
            i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            self.assertTrue(i.response.is_done())

        staff.side_effect = permissions
        modal = CountryCapModal(self.panel)
        modal.value._value = "17"
        await modal.on_submit(i)
        self.assertEqual(self.panel.values["country_cap"], 17)
        parent_edit = self.panel.message.edit.call_args.kwargs
        self.assertIn("Country cap: 17", parent_edit["embed"].footer.text)
        self.assertIs(parent_edit["view"], self.panel)
        self.assertIn("press **Save**", i.edit_original_response.call_args.kwargs["content"])
        self.panel.engine.save_config.assert_not_awaited()

        # Submitting a modal edits pending setup values; Save persists the cap.
        save = next(item for item in self.panel.children if getattr(item, "label", "") == "Save")
        save_i = modal_interaction()
        staff.side_effect = None
        await save.callback(save_i)
        self.assertEqual(self.panel.engine.save_config.call_args.args[1]["country_cap"], 17)

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_blank_cap_removes_limit_and_preserves_other_pending_values(self, staff):
        self.panel.values["country_cap"] = 17
        self.panel.values["club_limit"] = 7
        modal = CountryCapModal(self.panel)
        modal.value._value = "   "
        i = modal_interaction()
        await modal.on_submit(i)
        self.assertIsNone(self.panel.values["country_cap"])
        self.assertEqual(self.panel.values["club_limit"], 7)
        self.assertIn("cap removed", i.edit_original_response.call_args.kwargs["content"])

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_invalid_caps_give_feedback_without_changing_panel(self, staff):
        self.panel.values["country_cap"] = 17
        for value in ("0", "-1", "abc", "17.5"):
            with self.subTest(value=value):
                modal = CountryCapModal(self.panel)
                modal.value._value = value
                i = modal_interaction()
                await modal.on_submit(i)
                self.assertEqual(self.panel.values["country_cap"], 17)
                self.assertIn(
                    "positive whole number", i.edit_original_response.call_args.kwargs["content"]
                )
        self.panel.message.edit.assert_not_awaited()

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_club_limit_uses_same_acknowledgement_and_parent_update(self, staff):
        modal = ClubLimitModal(self.panel)
        modal.value._value = "4"
        i = modal_interaction()
        await modal.on_submit(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertEqual(self.panel.values["club_limit"], 4)
        self.panel.message.edit.assert_awaited_once()

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_permission_failure_is_visible_and_does_not_change_cap(self, staff):
        staff.side_effect = RuleError("Admin role required")
        modal = CountryCapModal(self.panel)
        modal.value._value = "17"
        i = modal_interaction()
        with self.assertRaises(RuleError) as raised:
            await modal.on_submit(i)
        await modal.on_error(i, raised.exception)
        self.assertIn("Admin role required", i.edit_original_response.call_args.kwargs["content"])
        self.assertIsNone(self.panel.values["country_cap"])
        self.panel.message.edit.assert_not_awaited()

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_other_members_and_closed_panels_are_rejected(self, staff):
        modal = CountryCapModal(self.panel)
        modal.value._value = "17"
        with self.assertRaisesRegex(RuleError, "Only the staff member"):
            await modal.on_submit(modal_interaction(uid=11))
        self.panel.stop()
        with self.assertRaisesRegex(RuleError, "expired or was closed"):
            await modal.on_submit(modal_interaction())
        staff.assert_not_awaited()
        self.assertIsNone(self.panel.values["country_cap"])

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_missing_setup_message_gives_reopen_instruction(self, staff):
        self.panel.message = None
        modal = CountryCapModal(self.panel)
        modal.value._value = "17"
        with self.assertRaisesRegex(RuleError, "Run /setup again"):
            await modal.on_submit(modal_interaction())
        self.assertIsNone(self.panel.values["country_cap"])

    @patch("bot.views.setup.require_staff", new_callable=AsyncMock)
    async def test_failed_panel_edit_restores_previous_cap(self, staff):
        self.panel.values["country_cap"] = 10
        self.panel.message.edit.side_effect = RuntimeError("message edit failed")
        modal = CountryCapModal(self.panel)
        modal.value._value = "17"
        with self.assertRaisesRegex(RuntimeError, "message edit failed"):
            await modal.on_submit(modal_interaction())
        self.assertEqual(self.panel.values["country_cap"], 10)
