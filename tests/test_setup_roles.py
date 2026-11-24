import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot.services.rules import RuleError
from bot.views.setup import SetupPanel
from test_postgres import Guild, Role


def interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=10),
        response=SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class SetupRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.engine = MagicMock()
        self.engine.save_config = AsyncMock()
        self.panel = SetupPanel(self.engine, 10, self.guild, None, {1, 2}, {8, 9})
        self.panel.section = "clubs"
        self.panel.build()

    def removal(self):
        return next(item for item in self.panel.children if item.row == 2)

    def button(self, label):
        return next(item for item in self.panel.children if getattr(item, "label", None) == label)

    async def test_removal_lists_only_configured_clubs_and_preserves_other_settings(self):
        selector = self.removal()
        self.assertIsInstance(selector, discord.ui.Select)
        self.assertEqual({option.value for option in selector.options}, {"8", "9"})
        self.assertEqual({option.label for option in selector.options}, {"Role 8", "Role 9"})
        self.panel.values["country_cap"] = 15
        selector._values = ["8"]
        await selector.callback(interaction())
        self.assertEqual(self.panel.values["clubs"], {9})
        self.assertEqual(self.panel.values["countries"], {1, 2})
        self.assertEqual(self.panel.values["country_cap"], 15)
        self.assertEqual([option.value for option in self.removal().options], ["9"])
        self.engine.save_config.assert_not_awaited()

        with patch("bot.views.setup.require_staff", new_callable=AsyncMock):
            await self.button("Save").callback(interaction())
        saved = self.engine.save_config.call_args.args[1]
        self.assertEqual(saved["clubs"], {9})
        self.assertEqual(saved["countries"], {1, 2})
        self.engine.start_reset.assert_not_called()

    async def test_additions_accumulate_and_update_removal_list(self):
        selector = next(item for item in self.panel.children if item.row == 1)
        selector._values = [self.guild.get_role(3)]
        await selector.callback(interaction())
        self.assertEqual(self.panel.values["clubs"], {3, 8, 9})
        self.assertEqual({option.value for option in self.removal().options}, {"3", "8", "9"})

    async def test_removal_pages_cover_all_roles_and_clamp_after_last_page_is_removed(self):
        self.panel.values["clubs"] = set(range(100, 151))
        for rid in self.panel.values["clubs"]:
            self.guild.role_map[rid] = Role(rid)
        self.panel.build()
        seen = set()
        for page in range(3):
            selector = self.removal()
            self.assertLessEqual(len(selector.options), 25)
            seen.update(option.value for option in selector.options)
            rows = self.panel.to_components()
            self.assertLessEqual(len(rows), 5)
            self.assertTrue(all(len(row["components"]) <= 5 for row in rows))
            if page < 2:
                await self.button("Next roles").callback(interaction())
        self.assertEqual(seen, {str(rid) for rid in range(100, 151)})
        self.assertTrue(self.button("Next roles").disabled)
        selector._values = ["150"]
        await selector.callback(interaction())
        self.assertEqual(self.panel.role_pages["clubs"], 1)
        self.assertEqual(len(self.removal().options), 25)
        await self.button("Previous roles").callback(interaction())
        self.assertEqual(self.panel.role_pages["clubs"], 0)
        self.assertTrue(self.button("Previous roles").disabled)

    async def test_deleted_role_can_be_removed_and_empty_list_is_disabled(self):
        self.panel.values["clubs"] = {999}
        self.panel.build()
        selector = self.removal()
        self.assertEqual(selector.options[0].label, "Deleted role (999)")
        selector._values = ["999"]
        await selector.callback(interaction())
        self.assertEqual(self.panel.values["clubs"], set())
        self.assertTrue(self.removal().disabled)
        self.panel.to_components()

    async def test_unconfigured_role_cannot_be_removed(self):
        selector = self.removal()
        selector._values = ["20"]
        with self.assertRaises(RuleError):
            await selector.callback(interaction())
        self.assertEqual(self.panel.values["clubs"], {8, 9})

    async def test_country_removal_and_cancel_do_not_change_saved_configuration(self):
        self.panel.section = "countries"
        self.panel.build()
        selector = self.removal()
        self.assertEqual({option.value for option in selector.options}, {"1", "2"})
        selector._values = ["1"]
        await selector.callback(interaction())
        self.assertEqual(self.panel.values["countries"], {2})
        self.assertEqual(self.panel.values["clubs"], {8, 9})
        await self.button("Cancel").callback(interaction())
        self.engine.save_config.assert_not_awaited()
        self.engine.start_reset.assert_not_called()
