import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.commands import StaffCommands
from bot.services.engine import Engine
from bot.services.rules import RuleError, goalkeeper_groups
from bot.views.common import Pages
from bot.views.setup import SetupPanel
from test_postgres import Guild, Role


class GoalkeeperGroupsTests(unittest.TestCase):
    def test_only_gk_role_holders_are_grouped_and_clubs_do_not_count_as_countries(self):
        guild = Guild()
        guild.player(100, (1, 3))
        guild.player(101, (2, 3))
        guild.player(102, (3, 8))
        guild.player(103, (1,))
        bot = guild.player(104, (3,))
        bot.bot = True
        grouped, unassigned = goalkeeper_groups(guild.members, 3, {1, 2})
        self.assertEqual(grouped, {1: [100], 2: [101]})
        self.assertEqual(unassigned, [102])

    def test_empty_country_and_multicountry_gk_are_reported_without_guessing(self):
        guild = Guild()
        member = guild.player(100, (1, 2, 3))
        grouped, unassigned = goalkeeper_groups([member, member], 3, {1, 2, 9})
        self.assertEqual(grouped, {1: [100], 2: [100], 9: []})
        self.assertEqual(unassigned, [])

    def test_no_configured_countries_lists_all_gks_as_unassigned(self):
        guild = Guild()
        guild.player(100, (3, 8))
        self.assertEqual(goalkeeper_groups(guild.members, 3, set()), ({}, [100]))


class GoalkeeperCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.guild.role_map[30] = Role(30)
        self.guild.get_role(1).name = "Spain"
        self.guild.get_role(2).name = "Italy"
        self.guild.get_role(3).name = "Norway"
        self.con = SimpleNamespace()

        @asynccontextmanager
        async def acquire():
            yield self.con

        self.engine = SimpleNamespace(
            config=AsyncMock(return_value={"international_gk_role_id": 30}),
            role_ids=AsyncMock(return_value={1, 2, 3}),
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

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_report_matches_requested_countries_and_unassigned_layout(self, staff):
        staff.side_effect = RuleError("Staff only")
        self.guild.player(100, (1,))  # Not a GK.
        self.guild.player(101, (2, 30))
        self.guild.player(102, (2, 30))
        self.guild.player(103, (3, 30))
        self.guild.player(104, (8, 30))  # A club role does not assign a country.
        self.guild.player(105, (9, 30))
        await self.cog.goalkeepers.callback(self.cog, self.i)
        lines = self.cog.pages.call_args.args[2]
        spain = lines.index("\n**<@&1>**")
        self.assertEqual(lines[spain + 1], "No GK")
        italy = lines.index("\n**<@&2>**")
        self.assertEqual(lines[italy + 1 : italy + 3], ["<@101>", "<@102>"])
        norway = lines.index("\n**<@&3>**")
        self.assertEqual(lines[norway + 1], "<@103>")
        unassigned = lines.index("\n**Unassigned goalkeepers**")
        self.assertEqual(lines[unassigned + 1 :], ["<@104>", "<@105>"])
        self.assertNotIn("<@100>", lines)
        self.assertNotIn("<@&8>", "\n".join(lines))
        self.assertEqual(self.cog.pages.call_args.kwargs["staff_only"], False)
        self.assertEqual(
            self.cog.pages.call_args.kwargs["footer"], "International GKs: 5 • Unassigned: 2"
        )
        staff.assert_not_awaited()

    async def test_missing_or_deleted_gk_role_gives_setup_instruction(self):
        for role in (None, 999):
            self.engine.config.return_value = {"international_gk_role_id": role}
            with self.assertRaisesRegex(RuleError, "International GK role in /setup"):
                await self.cog.goalkeepers.callback(self.cog, self.i)
        self.cog.pages.assert_not_awaited()

    async def test_incomplete_cache_is_filled_before_crosschecking_roles(self):
        self.guild.chunked = False
        member = self.guild.player(101, (2, 30))
        self.guild.members = []
        self.guild.chunk = AsyncMock(return_value=[member])
        await self.cog.goalkeepers.callback(self.cog, self.i)
        self.guild.chunk.assert_awaited_once()
        self.assertIn("<@101>", self.cog.pages.call_args.args[2])

    async def test_gks_on_disabled_countries_are_not_misreported_as_unassigned(self):
        self.guild.role_map[4] = Role(4)
        self.guild.player(101, (4, 30))
        self.engine.role_ids.side_effect = [{1, 2, 3}, {1, 2, 3, 4}]
        await self.cog.goalkeepers.callback(self.cog, self.i)
        lines = self.cog.pages.call_args.args[2]
        inactive = lines.index("\n**<@&4> — disabled country**")
        self.assertEqual(lines[inactive + 1], "<@101>")
        unassigned = lines.index("\n**Unassigned goalkeepers**")
        self.assertEqual(lines[unassigned + 1 :], ["None"])

    async def test_long_reports_keep_every_gk_and_country(self):
        for uid in range(100000000000000000, 100000000000000200):
            self.guild.player(uid, (2, 30))
        await self.cog.goalkeepers.callback(self.cog, self.i)
        view = Pages(
            None,
            77,
            "Goalkeepers",
            self.cog.pages.call_args.args[2],
            **self.cog.pages.call_args.kwargs,
        )
        self.assertGreater(len(view.pages), 1)
        text = "\n".join(view.pages)
        for member in self.guild.members:
            self.assertEqual(text.count(f"<@{member.id}>"), 1)
        self.assertIn("**<@&1>**\nNo GK", text)

    async def test_goalkeepers_command_is_server_only(self):
        self.i.guild = None
        with self.assertRaisesRegex(RuleError, "inside your Discord server"):
            await self.cog.goalkeepers.callback(self.cog, self.i)
        self.engine.config.assert_not_awaited()


class GoalkeeperSetupTests(unittest.IsolatedAsyncioTestCase):
    def make_setup(self, goalkeeper=3, old=None):
        guild = Guild()
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value=old),
            fetch=AsyncMock(return_value=[]),
            execute=AsyncMock(),
        )

        @asynccontextmanager
        async def context(*args):
            yield con

        con.transaction = context
        engine = Engine(SimpleNamespace(guild=context), None)
        engine.cycle = AsyncMock(return_value=1)
        engine.reconcile_waitlist = AsyncMock()
        engine.flag_capacity = AsyncMock()
        engine.audit = AsyncMock()
        values = dict(
            countries={1, 2},
            clubs={8},
            staff_role_id=None,
            opt_in_role_id=None,
            callups_channel_id=None,
            logs_channel_id=None,
            club_limit=5,
            international_gk_role_id=goalkeeper,
        )
        return engine, guild, con, values

    async def test_setup_saves_single_read_only_gk_role_without_position_mappings(self):
        engine, guild, con, values = self.make_setup()
        guild.role_map[3].managed = True  # Reporting does not require managing this role.
        await engine.save_config(guild, values, 77)
        write = next(
            c for c in con.execute.call_args_list if "INSERT INTO guild_config" in c.args[0]
        )
        self.assertEqual(write.args[-1], 3)
        self.assertFalse(any("position_" in str(c) for c in con.execute.call_args_list))
        self.assertEqual(engine.audit.call_args.kwargs["international_gk_role_id"], 3)

    async def test_country_or_club_cannot_also_be_the_international_gk_role(self):
        for role in (1, 8):
            engine, guild, con, values = self.make_setup(goalkeeper=role)
            with self.assertRaisesRegex(RuleError, "must be separate"):
                await engine.save_config(guild, values, 77)
            con.execute.assert_not_awaited()

    async def test_respun_marker_cannot_be_used_as_gk_role(self):
        engine, guild, _, values = self.make_setup(
            old={"respin_role_id": 3, "revision": 1, "resetting": False}
        )
        with self.assertRaisesRegex(RuleError, "Respun marker"):
            await engine.save_config(guild, values, 77, expected_revision=1)

    async def test_disabled_team_roles_cannot_be_repurposed_as_gk_role(self):
        engine, guild, con, values = self.make_setup()
        con.fetch.side_effect = [[{"role_id": 3}], []]
        with self.assertRaisesRegex(RuleError, "including disabled teams"):
            await engine.save_config(guild, values, 77)
        con.execute.assert_not_awaited()

    async def test_setup_has_one_optional_gk_selector_and_no_position_sections(self):
        panel = SetupPanel(MagicMock(), 77, Guild(), None, set(), set())
        section_ids = {option.value for option in panel.children[0].options}
        self.assertIn("goalkeepers", section_ids)
        self.assertFalse(any(key.startswith("pos_") for key in section_ids | set(panel.values)))
        self.assertFalse(any("position roles" in f.name for f in panel.embed().fields))
        panel.section = "goalkeepers"
        panel.build()
        selector = panel.children[1]
        self.assertEqual((selector.min_values, selector.max_values), (0, 1))
        panel.values["international_gk_role_id"] = 3
        selector._values = []
        i = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        await selector.callback(i)
        self.assertIsNone(panel.values["international_gk_role_id"])
