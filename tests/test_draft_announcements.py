import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.engine import Engine
from bot.services.rules import RuleError
from test_postgres import Guild


@asynccontextmanager
async def transaction():
    yield


class DraftAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, status="pending"):
        operation = dict(
            id=42,
            guild_id=50001,
            user_id=100,
            cycle_id=10,
            from_country_id=None,
            target_country_id=1,
            action="draft",
            actor_id=77,
            batch_id=5,
            override_type=None,
            clear_country_ids=None,
            status=status,
            attempts=0,
        )
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value=operation),
            fetchval=AsyncMock(return_value=False),
            execute=AsyncMock(),
            transaction=transaction,
        )
        engine = Engine(None, None)
        engine.config = AsyncMock(
            return_value={"opt_in_role_id": 21, "respin_role_id": None, "callups_channel_id": 30}
        )
        engine.role_ids = AsyncMock(return_value={1, 2})
        engine.outbox = AsyncMock()
        engine.audit = AsyncMock()
        return engine, con

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_successful_batch_player_queues_individual_mentions(self, apply):
        engine, con = self.make_engine()

        async def queue(*args):
            apply.assert_awaited_once()

        engine.outbox.side_effect = queue
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_awaited_once_with(
            con, 50001, "operation:42", 30, "**<@100> has been assigned to <@&1>!**"
        )
        # Individual announcements leave the separate squad reveal available.
        self.assertFalse(any("batch-player:" in str(c) for c in con.execute.call_args_list))

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_failed_player_is_not_announced(self, apply):
        engine, con = self.make_engine()
        apply.side_effect = RuleError("Missing role permissions")
        self.assertFalse(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_not_awaited()

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_completed_player_is_not_announced_again(self, apply):
        engine, con = self.make_engine("completed")
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_not_awaited()
        apply.assert_not_awaited()

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_failed_player_announces_when_recovery_succeeds(self, apply):
        engine, con = self.make_engine("failed")
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_awaited_once_with(
            con, 50001, "operation:42", 30, "**<@100> has been assigned to <@&1>!**"
        )

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_existing_country_uses_same_reassignment_message_for_every_action(self, apply):
        for action in ("draft", "manual", "respin", "respin_exception"):
            with self.subTest(action=action):
                engine, con = self.make_engine()
                con.fetchrow.return_value.update(
                    action=action, from_country_id=2, batch_id=5 if action == "draft" else None
                )
                engine.ensure_respin_role = AsyncMock(return_value=30)
                self.assertTrue(await engine.execute_operation(con, Guild(), 42))
                engine.outbox.assert_awaited_once_with(
                    con,
                    50001,
                    "operation:42",
                    30,
                    "**<@100> has been reassigned from <@&2> to <@&1>!**",
                )

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_manual_assignment_without_country_uses_assigned_message(self, apply):
        engine, con = self.make_engine()
        con.fetchrow.return_value.update(action="manual", batch_id=None)
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_awaited_once_with(
            con, 50001, "operation:42", 30, "**<@100> has been assigned to <@&1>!**"
        )

    @patch("bot.services.engine.apply_roles", new_callable=AsyncMock)
    async def test_same_country_reconciliation_still_counts_as_reassignment(self, apply):
        engine, con = self.make_engine()
        con.fetchrow.return_value.update(action="manual", from_country_id=1, batch_id=None)
        self.assertTrue(await engine.execute_operation(con, Guild(), 42))
        engine.outbox.assert_awaited_once_with(
            con, 50001, "operation:42", 30, "**<@100> has been reassigned to <@&1>!**"
        )
