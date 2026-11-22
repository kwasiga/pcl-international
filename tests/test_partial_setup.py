import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.engine import Engine
from bot.services.rules import RuleError, missing_setup


class PartialSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_setup_can_be_saved(self):
        con = SimpleNamespace(fetchrow=AsyncMock(return_value=None), execute=AsyncMock())

        @asynccontextmanager
        async def transaction():
            yield

        @asynccontextmanager
        async def guild_lock(gid):
            yield con

        con.transaction = transaction
        engine = Engine(SimpleNamespace(guild=guild_lock), None)
        engine.cycle = AsyncMock(return_value=1)
        engine.reconcile_waitlist = AsyncMock()
        engine.flag_capacity = AsyncMock()
        engine.audit = AsyncMock()
        values = dict(
            countries=set(),
            clubs=set(),
            staff_role_id=None,
            opt_in_role_id=None,
            callups_channel_id=None,
            logs_channel_id=None,
            club_limit=5,
        )
        await engine.save_config(SimpleNamespace(id=123, members=[]), values, 99)
        writes = [c for c in con.execute.call_args_list if "INSERT INTO guild_config" in c.args[0]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[1:], (123, None, None, None, None, 5))

    async def test_feature_reports_missing_callups_before_changing_roles(self):
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"resetting": False, "callups_channel_id": None})
        )
        with self.assertRaisesRegex(RuleError, "call-ups channel"):
            await Engine(None, None).config(con, 123, required=("callups_channel_id",))

    async def test_partial_configuration_can_be_read_without_full_setup(self):
        cfg = {"resetting": False, "callups_channel_id": None}
        con = SimpleNamespace(fetchrow=AsyncMock(return_value=cfg))
        self.assertEqual(await Engine(None, None).config(con, 123), cfg)

    async def test_audit_still_stored_without_log_channel(self):
        con = SimpleNamespace(
            fetchval=AsyncMock(return_value=1),
            fetchrow=AsyncMock(return_value={"logs_channel_id": None}),
        )
        engine = Engine(None, None)
        engine.outbox = AsyncMock()
        await engine.audit(con, 123, "configuration saved", 99)
        con.fetchval.assert_awaited_once()
        engine.outbox.assert_not_awaited()

    def test_missing_settings_summary_does_not_require_positions_or_clubs(self):
        self.assertEqual(
            missing_setup(
                {
                    "staff_role_id": 1,
                    "opt_in_role_id": 2,
                    "callups_channel_id": 3,
                    "logs_channel_id": 4,
                    "countries": {5},
                }
            ),
            [],
        )
        self.assertIn("admin role", missing_setup({}))
