import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands import StaffCommands
from bot.services.engine import Engine
from bot.services.rules import RuleError, respin_notice
from test_postgres import Guild


class RespinNoticeTests(unittest.TestCase):
    def test_first_and_second_reroll_show_remaining_allowance(self):
        self.assertIn("After this reroll succeeds: **2 remaining**", respin_notice(0))
        self.assertIn("After this reroll succeeds: **1 remaining**", respin_notice(1))

    def test_third_reroll_has_explicit_final_warning(self):
        notice = respin_notice(2)
        self.assertIn("**2/3**", notice)
        self.assertIn("THIRD AND FINAL", notice)
        self.assertIn("0 rerolls left", notice)

    def test_no_rerolls_left_blocks_normal_request(self):
        for count in (3, 4, 20):
            with self.subTest(count=count), self.assertRaisesRegex(RuleError, "no rerolls left"):
                respin_notice(count)

    def test_admin_override_explicitly_warns_about_exceeding_limit(self):
        notice = respin_notice(3, override=True)
        self.assertIn("ADMIN OVERRIDE", notice)
        self.assertIn("no rerolls remaining", notice)
        self.assertIn("will be logged", notice)


class RespinLimitEngineTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, used):
        guild = Guild()
        player = guild.player(100, (1,))
        admin = guild.player(77, (20,))
        con = SimpleNamespace(
            fetchval=AsyncMock(
                side_effect=lambda sql, *args: (
                    1 if "SELECT current_country_role_id" in sql else None
                )
            )
        )

        @asynccontextmanager
        async def context(*args):
            yield con

        con.transaction = context
        engine = Engine(SimpleNamespace(guild=context), None)
        engine.config = AsyncMock()
        engine.refresh_player = AsyncMock()
        engine.idle_player = AsyncMock()
        engine.cycle = AsyncMock(return_value=10)
        engine.respins_used = AsyncMock(return_value=used)
        engine.respin_target = AsyncMock(return_value=2)
        engine.prepare_operation = AsyncMock(return_value=42)
        engine.execute_operation = AsyncMock(return_value=True)
        return engine, con, player, admin

    async def test_third_reroll_is_allowed_and_uses_current_cycle_count(self):
        engine, con, player, _ = self.make_engine(2)
        self.assertTrue(
            await engine.assign(
                player, 2, 77, action="respin", expected_respins=2, expected_cycle=10
            )
        )
        engine.respins_used.assert_awaited_once_with(con, player.guild.id, 100, 10)
        self.assertFalse(engine.prepare_operation.call_args.kwargs["respin_override"])

    async def test_fourth_reroll_is_blocked_before_roles_or_operation_change(self):
        engine, _, player, _ = self.make_engine(3)
        with self.assertRaisesRegex(RuleError, "no rerolls left"):
            await engine.assign(player, 2, 77, action="respin")
        engine.prepare_operation.assert_not_awaited()
        engine.execute_operation.assert_not_awaited()

    async def test_changed_count_requires_a_new_confirmation(self):
        engine, _, player, _ = self.make_engine(2)
        with self.assertRaisesRegex(RuleError, "reroll count changed"):
            await engine.assign(player, 2, 77, action="respin", expected_respins=1)
        engine.prepare_operation.assert_not_awaited()

    @patch("bot.services.engine.require_staff", new_callable=AsyncMock)
    async def test_override_requires_admin_permission_and_is_journalled(self, require_admin):
        engine, con, player, admin = self.make_engine(3)
        self.assertTrue(
            await engine.assign(
                player, 2, 77, action="respin", respin_override=True, expected_respins=3
            )
        )
        require_admin.assert_awaited_once_with(engine.db, admin, admin=True, connection=con)
        self.assertTrue(engine.prepare_operation.call_args.kwargs["respin_override"])
        self.assertFalse(engine.prepare_operation.call_args.kwargs["override"])

    @patch("bot.services.engine.require_staff", new_callable=AsyncMock)
    async def test_revoked_admin_role_blocks_override(self, require_admin):
        engine, _, player, _ = self.make_engine(3)
        require_admin.side_effect = RuleError("Admin role required")
        with self.assertRaisesRegex(RuleError, "Admin role required"):
            await engine.assign(player, 2, 77, action="respin", respin_override=True)
        engine.prepare_operation.assert_not_awaited()
        engine.execute_operation.assert_not_awaited()

    async def test_proposal_includes_usage_and_cycle_without_changing_roles(self):
        engine, _, player, _ = self.make_engine(2)
        self.assertEqual(await engine.propose_respin(player), (1, 2, [], 10, 2))
        engine.prepare_operation.assert_not_awaited()
        engine.execute_operation.assert_not_awaited()

    async def test_exhausted_proposal_is_blocked_before_random_draw(self):
        engine, _, player, _ = self.make_engine(3)
        with self.assertRaisesRegex(RuleError, "no rerolls left"):
            await engine.propose_respin(player)
        engine.respin_target.assert_not_awaited()

    async def test_count_is_scoped_to_successful_history_in_current_guild_and_cycle(self):
        con = SimpleNamespace(fetchval=AsyncMock(return_value=2))
        self.assertEqual(await Engine(None, None).respins_used(con, 50001, 100, 10), 2)
        query, *args = con.fetchval.call_args.args
        self.assertEqual(args, [50001, 100, 10])
        self.assertIn("assignment_history", query)
        self.assertIn("action IN ('respin','respin_exception')", query)


class RespinLimitCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_command(self, used, *, target=2, exceptions=()):
        guild = Guild()
        player = guild.player(100, (1,))
        player.mention = "<@100>"
        i = SimpleNamespace(
            guild=guild,
            guild_id=guild.id,
            user=SimpleNamespace(id=77),
            response=SimpleNamespace(edit_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            original_response=AsyncMock(),
        )
        engine = SimpleNamespace(
            propose_respin=AsyncMock(return_value=(1, target, list(exceptions), 10, used)),
            assign=AsyncMock(return_value=True),
        )
        cog = StaffCommands(SimpleNamespace(engine=engine, db=None))
        cog.check = AsyncMock()
        cog.confirm = AsyncMock()
        return cog, i, player

    async def test_third_reroll_warns_before_confirmation_and_reports_zero_after(self):
        cog, i, player = self.make_command(2)
        await cog.respin.callback(cog, i, player)
        self.assertIn("THIRD AND FINAL", cog.confirm.call_args.args[1])
        cog.engine.assign.assert_not_awaited()
        result = await cog.confirm.call_args.args[2](i)
        self.assertIn("0 rerolls remaining", result)
        self.assertEqual(cog.engine.assign.call_args.kwargs["expected_respins"], 2)

    async def test_admin_override_requires_explicit_warning_and_confirmation(self):
        cog, i, player = self.make_command(3)
        await cog.respin.callback(cog, i, player, override_limit=True)
        self.assertIn("ADMIN OVERRIDE", cog.confirm.call_args.args[1])
        cog.engine.assign.assert_not_awaited()
        await cog.confirm.call_args.args[2](i)
        self.assertTrue(cog.engine.assign.call_args.kwargs["respin_override"])

    async def test_previous_country_exception_keeps_third_reroll_warning(self):
        cog, i, player = self.make_command(2, target=None, exceptions=[2])
        await cog.respin.callback(cog, i, player)
        self.assertIn("THIRD AND FINAL", i.followup.send.call_args.args[0])
        picker = i.followup.send.call_args.kwargs["view"]
        select = picker.children[0]
        select._values = ["2"]
        await select.callback(i)
        self.assertIn("THIRD AND FINAL", i.response.edit_message.call_args.kwargs["content"])
        cog.engine.assign.assert_not_awaited()
