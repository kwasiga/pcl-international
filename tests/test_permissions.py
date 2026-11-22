import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.permissions import require_staff
from bot.services.rules import RuleError


class Role:
    def __init__(self, rank):
        self.rank = rank

    def __lt__(self, other):
        return self.rank < other.rank


class Member:
    def __init__(self, rank=1, admin=False, uid=10):
        self.id = uid
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.top_role = Role(rank)
        self.guild = SimpleNamespace(id=123, owner_id=99, get_role=lambda _: Role(5))


class Context:
    def __init__(self, con):
        self.con = con

    async def __aenter__(self):
        return self.con

    async def __aexit__(self, *args):
        pass


class PermissionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.con = SimpleNamespace(fetchrow=AsyncMock(return_value={"staff_role_id": 5}))
        self.db = SimpleNamespace(pool=SimpleNamespace(acquire=lambda: Context(self.con)))
        self.patch = patch("bot.services.permissions.discord.Member", Member)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_designated_role_and_higher_allowed_for_all_commands(self):
        for rank in (5, 8):
            for admin_command in (False, True):
                await require_staff(self.db, Member(rank), admin=admin_command)

    async def test_below_role_denied_for_all_commands(self):
        for admin_command in (False, True):
            with self.assertRaises(RuleError):
                await require_staff(self.db, Member(4), admin=admin_command)

    async def test_discord_administrator_does_not_bypass_designated_role(self):
        for admin_command in (False, True):
            with self.assertRaises(RuleError):
                await require_staff(self.db, Member(4, admin=True), admin=admin_command)

    async def test_owner_does_not_bypass_designated_role_after_setup(self):
        for admin_command in (False, True):
            with self.assertRaises(RuleError):
                await require_staff(self.db, Member(4, uid=99), admin=admin_command)
        await require_staff(self.db, Member(5, uid=99), admin=True)

    async def test_initial_setup_allows_owner_or_discord_admin(self):
        self.con.fetchrow.return_value = None
        await require_staff(self.db, Member(uid=99), admin=True)
        await require_staff(self.db, Member(admin=True), admin=True)
        with self.assertRaises(RuleError):
            await require_staff(self.db, Member(8), admin=True)
        with self.assertRaises(RuleError):
            await require_staff(self.db, Member(admin=True))

    async def test_deleted_admin_role_denies_access(self):
        member = Member(8, admin=True, uid=99)
        member.guild.get_role = lambda _: None
        with self.assertRaises(RuleError):
            await require_staff(self.db, member, admin=True)

    async def test_permission_lookup_uses_current_guild(self):
        await require_staff(self.db, Member(5))
        self.assertEqual(self.con.fetchrow.call_args.args[-1], 123)

    async def test_dms_denied(self):
        with self.assertRaises(RuleError):
            await require_staff(self.db, SimpleNamespace(id=10))
