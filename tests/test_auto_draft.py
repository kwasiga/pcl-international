import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.main import PCLInternational
from bot.services.engine import Engine
from bot.services.rules import RuleError
from test_postgres import Guild


class AutomaticDraftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = Guild()
        self.player = self.guild.player(100, (8, 21))
        self.depth = 0
        self.commits = 0

        @asynccontextmanager
        async def transaction():
            self.depth += 1
            try:
                yield
            except Exception:
                raise
            else:
                self.commits += 1
            finally:
                self.depth -= 1

        self.con = SimpleNamespace(
            transaction=transaction,
            fetchrow=AsyncMock(return_value={"proposed_country_role_id": 2}),
        )
        self.engine = Engine(None, None)
        self.engine.config = AsyncMock(return_value={"callups_channel_id": 30})
        self.engine.enqueue = AsyncMock(return_value="pending")
        self.engine.prepare_operation = AsyncMock(return_value=42)
        self.engine.execute_operation = AsyncMock(return_value=True)
        self.engine.audit = AsyncMock()

    async def test_auto_assignment_uses_reserved_country_and_commits_before_discord(self):
        async def execute(*args):
            self.assertEqual(self.depth, 0)
            self.assertEqual(self.commits, 2)
            return True

        self.engine.execute_operation.side_effect = execute
        self.assertEqual(await self.engine.auto_draft_member(self.con, self.player), "assigned")
        self.engine.prepare_operation.assert_awaited_once_with(
            self.con, self.guild, 100, 2, "draft", None
        )
        self.engine.enqueue.assert_awaited_once_with(self.con, self.player, "opt_in", refresh=True)

    async def test_blocked_full_or_already_assigned_players_do_not_get_another_operation(self):
        for status in ("blocked", "unassigned", "active", "failed"):
            self.engine.enqueue.return_value = status
            self.assertEqual(await self.engine.auto_draft_member(self.con, self.player), status)
        self.engine.prepare_operation.assert_not_awaited()
        self.engine.execute_operation.assert_not_awaited()

    async def test_draft_pause_or_missing_channel_retains_waitlist_without_role_changes(self):
        for cfg in (
            {"callups_channel_id": 30, "paused_features": ["draft"]},
            {"callups_channel_id": None},
        ):
            self.engine.config.return_value = cfg
            self.assertEqual(await self.engine.auto_draft_member(self.con, self.player), "pending")
        self.assertEqual(self.commits, 2)
        self.engine.prepare_operation.assert_not_awaited()

    async def test_preparation_failure_keeps_committed_waitlist_for_retry(self):
        self.engine.prepare_operation.side_effect = RuleError("Country is now full")
        self.assertEqual(await self.engine.auto_draft_member(self.con, self.player), "pending")
        self.assertEqual(self.commits, 1)
        self.engine.execute_operation.assert_not_awaited()
        self.engine.audit.assert_awaited_once()

    async def test_role_failure_uses_existing_journal_recovery(self):
        self.engine.execute_operation.return_value = False
        self.assertEqual(await self.engine.auto_draft_member(self.con, self.player), "failed")
        self.assertEqual(self.commits, 2)

    async def test_role_event_auto_drafts_after_its_transaction_has_committed(self):
        self.con.fetchrow.return_value = {"opt_in_role_id": 21, "resetting": False}
        self.con.fetchval = AsyncMock(return_value=None)
        self.engine.role_ids = AsyncMock(
            side_effect=lambda con, gid, kind: {8} if kind == "club" else {1, 2}
        )

        async def auto(*args):
            self.assertEqual(self.depth, 0)
            self.assertEqual(self.commits, 1)
            return "assigned"

        self.engine.auto_draft_member = AsyncMock(side_effect=auto)

        @asynccontextmanager
        async def connection(*args):
            yield self.con

        self.engine.event_connection = connection
        self.assertTrue(
            await self.engine.member_update(
                SimpleNamespace(roles=[self.guild.get_role(8)]), self.player
            )
        )
        self.engine.auto_draft_member.assert_awaited_once_with(self.con, self.player)

    async def test_gateway_sends_successful_auto_assignment_announcement_immediately(self):
        engine = SimpleNamespace(
            member_update=AsyncMock(return_value=True), flush_outbox=AsyncMock()
        )
        bot = SimpleNamespace(engine=engine)
        before = SimpleNamespace()
        await PCLInternational.on_member_update(bot, before, self.player)
        engine.flush_outbox.assert_awaited_once_with(self.guild)

    async def test_backlog_prioritizes_gks_and_skips_staff_entries_without_optin(self):
        self.guild.player(101, (3, 21))
        self.guild.player(102, (8,))
        self.con.fetch = AsyncMock(
            return_value=[
                {"user_id": 100, "registration_source": "opt_in"},
                {"user_id": 102, "registration_source": "manual"},
                {"user_id": 101, "registration_source": "opt_in"},
            ]
        )
        self.con.fetchval = AsyncMock(return_value=1)
        self.engine.config.return_value.update(
            resetting=False, opt_in_role_id=21, international_gk_role_id=3
        )
        self.engine.auto_draft_member = AsyncMock(return_value="assigned")

        @asynccontextmanager
        async def connection(*args):
            yield self.con

        self.engine.db = SimpleNamespace(guild=connection)
        await self.engine.process_auto_drafts(self.guild)
        self.assertEqual(
            [call.args[1].id for call in self.engine.auto_draft_member.call_args_list], [101, 100]
        )


class StableReservationTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, choices):
        guild = Guild()
        player = guild.player(100, (8, 21))
        existing = dict(id=5, status="pending", proposed_country_role_id=1)
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value=existing),
            fetchval=AsyncMock(return_value=None),
            execute=AsyncMock(),
        )
        engine = Engine(None, None)
        engine.config = AsyncMock()
        engine.idle_player = AsyncMock()
        engine.refresh_player = AsyncMock(return_value=(8, False))
        engine.role_ids = AsyncMock(return_value={1, 2})
        engine.available = AsyncMock(side_effect=choices)
        engine.audit = AsyncMock()
        return engine, con, player

    async def test_valid_reservation_is_kept_without_second_registration_log(self):
        engine, con, player = await self.fixture([1])
        self.assertEqual(await engine.enqueue(con, player, "opt_in", refresh=True), "pending")
        engine.available.assert_awaited_once_with(
            con, player.guild, 100, excluded={2}, balance=False
        )
        engine.audit.assert_not_awaited()

    async def test_invalid_reservation_is_replaced_and_logged_as_an_update(self):
        engine, con, player = await self.fixture([None, 2])
        self.assertEqual(await engine.enqueue(con, player, "opt_in", refresh=True), "pending")
        self.assertEqual(engine.audit.call_args.args[2], "waitlist updated")
        self.assertEqual(engine.audit.call_args.kwargs["country"], 2)
