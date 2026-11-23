import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.services.engine import Engine
from test_postgres import Guild


class AssignmentRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_respun_role_is_created_without_permissions_and_reused(self):
        guild = Guild()
        con = SimpleNamespace(execute=AsyncMock())
        engine = Engine(None, None)
        cfg = {"respin_role_id": None, "opt_in_role_id": 21, "staff_role_id": 20}
        engine.config = AsyncMock(return_value=cfg)
        engine.role_ids = AsyncMock(return_value={1, 2, 8, 9})
        original = guild.create_role
        guild.create_role = AsyncMock(side_effect=original)
        self.assertEqual(await engine.ensure_respin_role(con, guild), 30)
        self.assertEqual(await engine.ensure_respin_role(con, guild), 30)
        guild.create_role.assert_awaited_once()
        call = guild.create_role.call_args.kwargs
        self.assertEqual(call["name"], "Respun")
        self.assertEqual(call["permissions"].value, 0)
        self.assertFalse(call["mentionable"])
        self.assertFalse(call["hoist"])

    async def test_saved_marker_role_is_reused_after_restart(self):
        guild = Guild()
        marker = await guild.create_role(name="Respun")
        guild.create_role = AsyncMock()
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"respin_role_id": marker.id})
        con = SimpleNamespace(execute=AsyncMock())
        self.assertEqual(await engine.ensure_respin_role(con, guild), marker.id)
        guild.create_role.assert_not_awaited()

    async def test_sync_cleanup_skips_unassigned_players(self):
        guild = Guild()
        member = guild.player(100, (21,))
        engine = Engine(None, None)
        con = SimpleNamespace(fetchval=AsyncMock())
        await engine.cleanup_assignment_roles(
            con, guild, member, {"current_country_role_id": None}, {}
        )
        con.fetchval.assert_not_awaited()

    async def test_sync_cleanup_skips_an_already_clean_assignment(self):
        guild = Guild()
        member = guild.player(100, (1,))
        engine = Engine(None, None)
        con = SimpleNamespace(fetchval=AsyncMock())
        await engine.cleanup_assignment_roles(
            con,
            guild,
            member,
            {"current_country_role_id": 1, "has_respun": False},
            {"respin_role_id": None, "opt_in_role_id": 21},
        )
        con.fetchval.assert_not_awaited()
