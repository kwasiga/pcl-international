import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.commands import StaffCommands
from bot.services.engine import Engine
from bot.services.rules import PlayerBannedError, RuleError
from bot.views.common import Pages
from test_postgres import Guild


@asynccontextmanager
async def context(value=None):
    yield value


class BanServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.player = self.guild.player(100, (1, 21))
        self.con = SimpleNamespace(
            transaction=context, execute=AsyncMock(), fetchval=AsyncMock(return_value=100)
        )
        self.engine = Engine(SimpleNamespace(guild=lambda _: context(self.con)), None)
        self.engine.config = AsyncMock()
        self.engine.is_banned = AsyncMock(return_value=False)
        self.engine.idle_player = AsyncMock()
        self.engine.prepare_operation = AsyncMock(return_value=42)
        self.engine.execute_operation = AsyncMock(return_value=True)
        self.engine.audit = AsyncMock()

    async def test_ban_is_committed_and_waitlist_removed_before_role_changes_start(self):
        committed = False

        @asynccontextmanager
        async def transaction():
            nonlocal committed
            yield
            committed = True

        async def apply(*args):
            self.assertTrue(committed)
            writes = [call.args[0] for call in self.con.execute.call_args_list]
            self.assertTrue(any("INSERT INTO player_bans" in sql for sql in writes))
            self.assertTrue(
                any("status='removed',reservation_active=FALSE" in sql for sql in writes)
            )
            return False  # Discord cleanup can fail without losing the persisted ban.

        self.con.transaction = transaction
        self.engine.execute_operation.side_effect = apply
        self.assertFalse(await self.engine.ban_player(self.player, 77, "Inactive"))
        self.engine.prepare_operation.assert_awaited_once_with(
            self.con, self.guild, 100, None, "ban", 77
        )
        self.engine.audit.assert_awaited_once_with(
            self.con, self.guild.id, "player banned", 77, 100, reason="Inactive"
        )

    async def test_duplicate_ban_and_invalid_reasons_do_not_create_operations(self):
        self.engine.is_banned.return_value = True
        with self.assertRaisesRegex(RuleError, "already banned"):
            await self.engine.ban_player(self.player, 77)
        for reason in (" ", "x" * 501):
            with self.assertRaisesRegex(RuleError, "1 and 500"):
                await self.engine.ban_player(self.player, 77, reason)
        self.engine.prepare_operation.assert_not_awaited()
        self.con.execute.assert_not_awaited()

    async def test_unban_does_not_restore_roles_or_requeue_and_requires_cleanup_to_finish(self):
        await self.engine.unban_player(self.guild, 100, 77)
        self.engine.idle_player.assert_awaited_once_with(self.con, self.guild.id, 100)
        self.engine.audit.assert_awaited_once_with(
            self.con, self.guild.id, "player unbanned", 77, 100
        )
        self.engine.prepare_operation.assert_not_awaited()
        self.engine.execute_operation.assert_not_awaited()
        self.con.execute.assert_not_awaited()
        self.engine.idle_player.side_effect = RuleError("Use /sync")
        self.con.fetchval.reset_mock()
        with self.assertRaisesRegex(RuleError, "/sync"):
            await self.engine.unban_player(self.guild, 100, 77)
        self.con.fetchval.assert_not_awaited()

    async def test_unban_reports_player_missing_from_list(self):
        self.con.fetchval.return_value = None
        with self.assertRaisesRegex(RuleError, "not on the ban list"):
            await self.engine.unban_player(self.guild, 100, 77)
        self.engine.audit.assert_not_awaited()

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_bans_block_optins_respins_manual_overrides_and_unfinished_assignments(
        self, roles
    ):
        self.con.fetchrow = AsyncMock(
            return_value={
                "status": "pending",
                "action": "draft",
                "target_country_id": 2,
                "user_id": 100,
            }
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(self.con)), None)
        engine.config = AsyncMock(return_value={})
        for action in (
            lambda: engine.enqueue(self.con, self.player),
            lambda: engine.propose_respin(self.player, override_limit=True),
            lambda: engine.needs_override(self.player, 2),
            lambda: engine.prepare_operation(
                self.con, self.guild, 100, 2, "manual", 77, override=True
            ),
            lambda: engine.execute_operation(self.con, self.guild, 42),
        ):
            with self.assertRaises(PlayerBannedError):
                await action()
        roles.assert_not_awaited()
        self.con.execute.assert_not_awaited()

    async def test_banned_returning_member_never_gets_country_restored(self):
        self.con.fetchrow = AsyncMock(return_value={"paused": False, "resetting": False})
        self.con.fetchval.side_effect = lambda sql, *args: 1 if "player_bans" in sql else None
        engine = Engine(SimpleNamespace(guild=lambda _: context(self.con)), None)
        engine.refresh_player = AsyncMock()
        engine.execute_operation = AsyncMock()
        await engine.member_joined(self.player)
        engine.execute_operation.assert_not_awaited()
        self.con.execute.assert_not_awaited()


class BanCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.player = self.guild.player(100)
        self.player.mention = "<@100>"
        self.player.display_name = "Kayge"
        self.con = SimpleNamespace(fetch=AsyncMock(return_value=[]))
        self.engine = SimpleNamespace(
            ban_player=AsyncMock(return_value=True),
            unban_player=AsyncMock(),
            config=AsyncMock(),
        )
        self.cog = StaffCommands(
            SimpleNamespace(
                engine=self.engine,
                db=SimpleNamespace(pool=SimpleNamespace(acquire=lambda: context(self.con))),
            )
        )
        self.cog.check = AsyncMock()
        self.cog.pages = AsyncMock()
        self.i = SimpleNamespace(
            guild=self.guild,
            guild_id=self.guild.id,
            user=SimpleNamespace(id=77),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_ban_calls_draft_service_and_explains_failure_recovery(self):
        self.engine.ban_player.return_value = False
        await self.cog.ban_player.callback(self.cog, self.i, self.player, "Inactive")
        self.cog.check.assert_awaited_once_with(self.i)
        self.engine.ban_player.assert_awaited_once_with(self.player, 77, "Inactive")
        self.assertIn("/sync", self.i.followup.send.call_args.args[0])

    async def test_unban_accepts_stored_ids_and_mentions_including_departed_players(self):
        for value in ("100", "<@100>", "<@!100>", "999"):
            await self.cog.unban_player.callback(self.cog, self.i, value)
            self.engine.unban_player.assert_awaited_with(self.guild, int(value.strip("<@!>")), 77)
        for value in ("Kayge", "0", "-1", "9" * 30, "9223372036854775808"):
            with self.assertRaises(RuleError):
                await self.cog.unban_player.callback(self.cog, self.i, value)

    async def test_bans_are_private_staff_pages_with_reasons_and_eight_per_page(self):
        self.con.fetch.return_value = [
            dict(
                user_id=uid,
                player_name=f"Player {uid}",
                reason="Inactive",
                actor_id=77,
                created_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
                removal_pending=uid == 100,
            )
            for uid in range(100, 117)
        ]
        await self.cog.bans.callback(self.cog, self.i)
        args, kwargs = self.cog.pages.call_args
        view = Pages(self.cog.db, 77, args[1], args[2], **kwargs)
        self.assertTrue(view.staff_only)
        self.assertEqual(len(view.pages), 3)
        self.assertIn("Role removal awaiting /sync", args[2][0])
        self.assertIn("Reason: Inactive", args[2][0])
        self.assertEqual(kwargs["footer"], "Banned players: 17")
        self.cog.check.assert_awaited_once_with(self.i)

    async def test_all_ban_commands_require_staff_before_reading_or_changing_players(self):
        self.cog.check.side_effect = RuleError("Admin role required")
        for action in (
            lambda: self.cog.ban_player.callback(self.cog, self.i, self.player),
            lambda: self.cog.unban_player.callback(self.cog, self.i, "100"),
            lambda: self.cog.bans.callback(self.cog, self.i),
        ):
            with self.assertRaisesRegex(RuleError, "Admin role required"):
                await action()
        self.engine.ban_player.assert_not_awaited()
        self.engine.unban_player.assert_not_awaited()
        self.con.fetch.assert_not_awaited()

    @patch("bot.commands.require_staff", new_callable=AsyncMock)
    async def test_autocomplete_is_staff_only_and_includes_departed_player_names(self, staff):
        staff.side_effect = RuleError("Admin role required")
        self.assertEqual(await self.cog.banned_players(self.i, ""), [])
        self.con.fetch.assert_not_awaited()
        staff.side_effect = None
        self.con.fetch.return_value = [
            {"user_id": 100, "player_name": "Kayge"},
            {"user_id": 999, "player_name": "Departed"},
        ]
        choices = await self.cog.banned_players(self.i, "")
        self.assertEqual([choice.value for choice in choices], ["100", "999"])
        self.assertIn("Departed", choices[1].name)
