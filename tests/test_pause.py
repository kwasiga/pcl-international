import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.commands import StaffCommands
from bot.main import PCLInternational
from bot.services.engine import Engine
from bot.services.rules import BotPausedError, RuleError
import test_draft_announcements
from test_postgres import Guild


@asynccontextmanager
async def context(value=None):
    yield value


class PauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_blocks_changes_but_allows_explicit_read_only_access(self):
        cfg = {"paused": True, "resetting": False}
        con = SimpleNamespace(fetchrow=AsyncMock(return_value=cfg))
        engine = Engine(None, None)
        with self.assertRaises(BotPausedError):
            await engine.config(con, 50001)
        self.assertEqual(await engine.config(con, 50001, allow_paused=True), cfg)

    async def test_mutating_commands_are_blocked_at_service_layer(self):
        guild = Guild()
        player = guild.player(100, (1,))
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"paused": True, "resetting": False}),
            execute=AsyncMock(),
            transaction=context,
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        engine.execute_operation = AsyncMock()
        actions = [
            lambda: engine.assign(player, 2, 77),
            lambda: engine.assign(player, 2, 77, action="respin", respin_override=True),
            lambda: engine.register(player),
            lambda: engine.remove_waitlist(player, 77),
            lambda: engine.propose_respin(player),
            lambda: engine.set_club(player, guild.get_role(8), 77),
            lambda: engine.draft(guild, 77),
            lambda: engine.release_country(guild, 77),
            lambda: engine.clear_countries(guild, 77),
            lambda: engine.start_reset(guild, 77),
        ]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(BotPausedError):
                await action()
        con.execute.assert_not_awaited()
        engine.execute_operation.assert_not_awaited()

    async def test_setup_saves_are_blocked_without_changing_config(self):
        guild = Guild()
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"paused": True}),
            execute=AsyncMock(),
            transaction=context,
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        values = dict(
            countries={1, 2},
            clubs={8},
            staff_role_id=None,
            opt_in_role_id=None,
            callups_channel_id=None,
            logs_channel_id=None,
            club_limit=5,
        )
        with self.assertRaises(BotPausedError):
            await engine.save_config(guild, values, 77)
        con.execute.assert_not_awaited()

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_paused_operations_stay_pending_without_failed_status_or_role_changes(
        self, apply
    ):
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"status": "pending"}), execute=AsyncMock()
        )
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"paused": True})
        with self.assertRaises(BotPausedError):
            await engine.execute_operation(con, Guild(), 42)
        apply.assert_not_awaited()
        con.execute.assert_not_awaited()

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_role_operation_already_in_flight_finishes_safely_after_pause(self, apply):
        engine, con = test_draft_announcements.DraftAnnouncementTests().make_engine()
        cfg = engine.config.return_value

        async def pause_during_role_change(*args, **kwargs):
            cfg["paused"] = True

        apply.side_effect = pause_during_role_change
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_awaited_once()
        self.assertTrue(
            any("INSERT INTO assignment_history" in c.args[0] for c in con.execute.call_args_list)
        )

    async def test_draft_stops_before_next_player_and_preserves_frozen_batch(self):
        guild = Guild()
        rows = [{"user_id": uid, "proposed_country_role_id": 1} for uid in (100, 101, 102)]
        con = SimpleNamespace(
            transaction=context,
            fetchval=AsyncMock(side_effect=[None, 5]),
            fetch=AsyncMock(side_effect=[rows, [{"id": n} for n in (1, 2, 3)]]),
            execute=AsyncMock(),
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        engine.config = AsyncMock()
        engine.cycle = AsyncMock(return_value=10)
        engine.reconcile_waitlist = AsyncMock()
        engine.prepare_operation = AsyncMock()
        engine.execute_operation = AsyncMock(side_effect=[True, BotPausedError()])
        engine.finish_batches = AsyncMock()
        with self.assertRaises(BotPausedError):
            await engine.draft(guild, 77)
        self.assertEqual(engine.prepare_operation.await_count, 3)
        self.assertEqual(engine.execute_operation.await_count, 2)
        engine.finish_batches.assert_not_awaited()

    async def test_recovery_and_announcement_delivery_do_nothing_while_paused(self):
        guild = Guild()
        con = SimpleNamespace(
            fetchval=AsyncMock(return_value=True), fetch=AsyncMock(), execute=AsyncMock()
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        await engine.recover_locked(con, guild)
        await engine.flush_outbox(guild)
        con.fetch.assert_not_awaited()
        con.execute.assert_not_awaited()
        guild.channel.send.assert_not_awaited()

    async def test_announcement_flush_stops_before_next_message(self):
        guild = Guild()
        rows = [
            dict(id=n, channel_id=30, payload=f"message {n}", dedupe_key=f"operation:{n}")
            for n in (1, 2)
        ]
        con = SimpleNamespace(fetch=AsyncMock(return_value=rows), execute=AsyncMock())
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        engine.is_paused = AsyncMock(side_effect=[False, False, True])
        await engine.flush_outbox(guild)
        guild.channel.send.assert_awaited_once()
        self.assertEqual(guild.channel.send.call_args.args[0], "message 1")
        con.execute.assert_awaited_once()

    @patch("bot.services.engine.require_staff", new_callable=AsyncMock)
    async def test_pause_bypasses_long_running_guild_lock_but_rechecks_admin_permission(
        self, staff
    ):
        guild = Guild()
        admin = guild.player(77, (20,))
        con = SimpleNamespace(transaction=context, fetchval=AsyncMock(return_value=guild.id))
        db = SimpleNamespace(
            pool=SimpleNamespace(acquire=lambda: context(con)),
            guild=MagicMock(side_effect=AssertionError("must not wait for guild lock")),
        )
        engine = Engine(db, None)
        engine.config = AsyncMock(return_value={"paused": False})
        engine.audit = AsyncMock()
        self.assertTrue(await engine.set_paused(guild, 77, True))
        staff.assert_awaited_once_with(db, admin, admin=True, connection=con)
        db.guild.assert_not_called()
        self.assertEqual(con.fetchval.call_args.args[1:], (guild.id, True, 77))
        engine.audit.assert_awaited_once_with(con, guild.id, "bot paused", 77)

    @patch("bot.services.engine.require_staff", new_callable=AsyncMock)
    async def test_unauthorized_pause_cannot_change_state(self, staff):
        guild = Guild()
        guild.player(77, (8,))
        con = SimpleNamespace(transaction=context, fetchval=AsyncMock())
        db = SimpleNamespace(pool=SimpleNamespace(acquire=lambda: context(con)))
        engine = Engine(db, None)
        engine.config = AsyncMock()
        staff.side_effect = RuleError("Admin role required")
        with self.assertRaisesRegex(RuleError, "Admin role required"):
            await engine.set_paused(guild, 77, True)
        con.fetchval.assert_not_awaited()

    async def test_paused_member_events_are_queued_without_other_changes(self):
        guild = Guild()
        player = guild.player(100, (21,))
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"paused": True}),
            fetchval=AsyncMock(return_value=True),
            execute=AsyncMock(),
        )
        engine = Engine(SimpleNamespace(guild=lambda _: context(con)), None)
        await engine.member_update(SimpleNamespace(roles=[]), player)
        await engine.member_left(player)
        await engine.member_joined(player)
        self.assertEqual(con.execute.await_count, 3)
        self.assertTrue(
            all("INSERT INTO paused_member_events" in c.args[0] for c in con.execute.call_args_list)
        )
        self.assertTrue(con.execute.call_args.args[-1])  # Rejoin needs restoration after resume.

    async def test_resumed_event_replay_rechecks_actual_optin_and_uses_versioned_deletion(self):
        guild = Guild()
        player = guild.player(100, (21,))
        con = SimpleNamespace(
            fetch=AsyncMock(return_value=[{"user_id": 100, "needs_restore": False, "version": 3}]),
            execute=AsyncMock(),
        )
        engine = Engine(None, None)
        engine.is_paused = AsyncMock(return_value=False)
        engine.config = AsyncMock(return_value={"opt_in_role_id": 21})
        engine.refresh_player = AsyncMock()
        engine.enqueue = AsyncMock()
        engine.reconcile_waitlist = AsyncMock()
        await engine.replay_member_events(con, guild)
        engine.enqueue.assert_awaited_once_with(con, player, "opt_in", refresh=True)
        self.assertEqual(con.execute.call_args.args[1:], (guild.id, 100, 3))
        engine.reconcile_waitlist.assert_awaited_once_with(con, guild)

    async def test_startup_maintenance_skips_paused_guild(self):
        con = SimpleNamespace()
        engine = SimpleNamespace(
            is_paused=AsyncMock(return_value=True),
            recover=AsyncMock(),
            sync=AsyncMock(),
            flush_outbox=AsyncMock(),
        )
        bot = SimpleNamespace(
            guilds=[Guild()],
            engine=engine,
            db=SimpleNamespace(pool=SimpleNamespace(acquire=lambda: context(con))),
            boot_reconciled=set(),
        )
        await PCLInternational.maintenance.coro(bot)
        engine.recover.assert_not_awaited()
        engine.sync.assert_not_awaited()
        engine.flush_outbox.assert_not_awaited()

    async def test_pause_and_resume_commands_require_admin_and_resume_schedules_reconciliation(
        self,
    ):
        engine = SimpleNamespace(set_paused=AsyncMock(return_value=True))
        bot = SimpleNamespace(engine=engine, db=None, boot_reconciled={50001})
        cog = StaffCommands(bot)
        cog.check = AsyncMock()
        i = SimpleNamespace(
            guild=Guild(),
            guild_id=50001,
            user=SimpleNamespace(id=77),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await cog.pause.callback(cog, i)
        cog.check.assert_awaited_with(i, True)
        engine.set_paused.assert_awaited_with(i.guild, 77, True)
        await cog.resume.callback(cog, i)
        cog.check.assert_awaited_with(i, True)
        engine.set_paused.assert_awaited_with(i.guild, 77, False)
        self.assertNotIn(50001, bot.boot_reconciled)
