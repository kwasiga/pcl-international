import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.roles import apply_roles, validate_role
from bot.services.rules import RuleError


class Role:
    def __init__(self, rid):
        self.id = rid

    def __eq__(self, other):
        return isinstance(other, Role) and self.id == other.id

    def __ge__(self, other):
        return self.id >= other.id

    managed = False

    def is_default(self):
        return False


class RoleSagaTests(unittest.IsolatedAsyncioTestCase):
    async def test_ban_removes_country_optin_and_respun_but_keeps_unrelated_roles(self):
        guild, member, events = self.fixture([1, 3, 4, 5])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            None,
            "PCL International: ban",
            opt_in_role_id=4,
            respin_role_id=5,
            clear_respun=True,
            clear_opt_in=True,
        )
        self.assertEqual([role.id for role in member.roles], [3])
        self.assertEqual(events, [("remove", 1), ("remove", 5), ("remove", 4)])

    async def test_ban_cleanup_with_no_country_still_removes_optin(self):
        guild, member, events = self.fixture([3, 4])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            None,
            "PCL International: ban",
            opt_in_role_id=4,
            clear_opt_in=True,
        )
        self.assertEqual([role.id for role in member.roles], [3])
        self.assertEqual(events, [("remove", 4)])

    def fixture(self, initial):
        roles = {rid: Role(rid) for rid in (1, 2, 3, 4, 5)}
        member = SimpleNamespace(id=10, roles=[roles[r] for r in initial], top_role=Role(3))
        events = []

        async def add(role, **kwargs):
            events.append(("add", role.id))
            member.roles.append(role)

        async def remove(role, **kwargs):
            events.append(("remove", role.id))
            member.roles.remove(role)

        member.add_roles, member.remove_roles = (
            AsyncMock(side_effect=add),
            AsyncMock(side_effect=remove),
        )
        guild = SimpleNamespace(
            owner_id=999,
            me=SimpleNamespace(
                top_role=Role(100), guild_permissions=SimpleNamespace(manage_roles=True)
            ),
            get_role=roles.get,
            fetch_member=AsyncMock(return_value=member),
        )
        return guild, member, events

    @patch("bot.services.roles.validate_role")
    async def test_add_new_before_removing_old_and_verify(self, validate):
        guild, member, events = self.fixture([1, 3])
        await apply_roles(guild, 10, {1, 2}, 2, "test")
        self.assertEqual(events, [("add", 2), ("remove", 1)])
        self.assertEqual([r.id for r in member.roles], [3, 2])
        self.assertEqual(guild.fetch_member.await_count, 2)

    @patch("bot.services.roles.validate_role")
    async def test_recovery_finishes_partial_transfer(self, validate):
        guild, member, events = self.fixture([1, 2, 3])
        await apply_roles(guild, 10, {1, 2}, 2, "recover")
        self.assertEqual(events, [("remove", 1)])

    @patch("bot.services.roles.validate_role")
    async def test_replaying_completed_role_state_is_idempotent(self, validate):
        guild, member, events = self.fixture([2, 3])
        await apply_roles(guild, 10, {1, 2}, 2, "recover")
        self.assertEqual(events, [])

    @patch("bot.services.roles.validate_role")
    async def test_reset_removes_all_managed_roles_only(self, validate):
        guild, member, events = self.fixture([1, 2, 3])
        await apply_roles(guild, 10, {1, 2}, None, "reset")
        # Discord Member.roles returns a fresh list; the fake must emulate it for mutation.
        self.assertEqual({r.id for r in member.roles}, {3})

    @patch("bot.services.roles.validate_role")
    async def test_failed_remote_verification_is_retryable(self, validate):
        guild, member, events = self.fixture([1])
        member.add_roles = AsyncMock()  # Simulate concurrent role edit / no applied change.
        with self.assertRaises(RuleError):
            await apply_roles(guild, 10, {1, 2}, 2, "test")

    @patch("bot.services.roles.validate_role")
    async def test_failed_add_preserves_previous_country(self, validate):
        guild, member, events = self.fixture([1])
        member.add_roles = AsyncMock(side_effect=RuleError("forbidden"))
        with self.assertRaises(RuleError):
            await apply_roles(guild, 10, {1, 2}, 2, "test")
        self.assertEqual([r.id for r in member.roles], [1])
        member.remove_roles.assert_not_awaited()

    async def test_higher_ranked_member_can_receive_lower_country_role(self):
        guild, member, events = self.fixture([1, 3])
        member.top_role = Role(1000)
        await apply_roles(guild, member.id, {1, 2}, 2, "test")
        self.assertEqual(events, [("add", 2), ("remove", 1)])

    async def test_server_owner_can_receive_lower_country_role(self):
        guild, member, events = self.fixture([3])
        guild.owner_id = member.id
        await apply_roles(guild, member.id, {1, 2}, 2, "test")
        self.assertEqual(events, [("add", 2)])

    async def test_country_role_above_bot_is_still_blocked(self):
        guild, member, events = self.fixture([1])
        guild.me.top_role = Role(2)
        with self.assertRaises(RuleError):
            await apply_roles(guild, member.id, {1, 2}, 2, "test")
        self.assertEqual(events, [])

    async def test_unmanageable_old_role_is_rejected_before_adding_target(self):
        guild, member, events = self.fixture([1, 3])
        guild.me.top_role = Role(3)
        with self.assertRaises(RuleError):
            await apply_roles(guild, member.id, {2, 3}, 2, "test")
        self.assertEqual(events, [])

    async def test_missing_manage_roles_is_still_blocked(self):
        guild, member, events = self.fixture([1])
        guild.me.guild_permissions.manage_roles = False
        with self.assertRaises(RuleError):
            validate_role(guild, guild.get_role(2))
        self.assertEqual(events, [])

    async def test_successful_assignment_removes_opt_in(self):
        guild, member, events = self.fixture([3])
        await apply_roles(guild, member.id, {1, 2}, 1, "draft", opt_in_role_id=3)
        self.assertEqual(events, [("add", 1), ("remove", 3)])
        self.assertEqual({r.id for r in member.roles}, {1})

    async def test_confirmed_respin_adds_marker_and_removes_opt_in(self):
        guild, member, events = self.fixture([1, 3])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            2,
            "respin",
            opt_in_role_id=3,
            respin_role_id=4,
            mark_respun=True,
        )
        self.assertEqual({r.id for r in member.roles}, {2, 4})
        self.assertEqual(events, [("add", 2), ("remove", 1), ("add", 4), ("remove", 3)])

    async def test_auxiliary_role_cleanup_retry_is_idempotent(self):
        guild, member, events = self.fixture([2, 3, 4])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            2,
            "recover",
            opt_in_role_id=3,
            respin_role_id=4,
            mark_respun=True,
        )
        self.assertEqual(events, [("remove", 3)])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            2,
            "recover",
            opt_in_role_id=3,
            respin_role_id=4,
            mark_respun=True,
        )
        self.assertEqual(events, [("remove", 3)])

    async def test_remove_clears_marker_but_preserves_other_roles(self):
        guild, member, events = self.fixture([1, 3, 4])
        await apply_roles(
            guild, member.id, {1, 2}, None, "remove", respin_role_id=4, clear_respun=True
        )
        self.assertEqual({r.id for r in member.roles}, {3})

    async def test_unmanageable_optin_blocks_before_country_change(self):
        guild, member, events = self.fixture([3])
        guild.me.top_role = Role(3)
        with self.assertRaises(RuleError):
            await apply_roles(guild, member.id, {1, 2}, 1, "draft", opt_in_role_id=3)
        self.assertEqual(events, [])

    async def test_marker_failure_stays_retryable_and_keeps_optin(self):
        guild, member, events = self.fixture([1, 3])
        original = member.add_roles

        async def add(role, **kwargs):
            if role.id == 4:
                raise RuleError("marker cannot be added")
            await original(role, **kwargs)

        member.add_roles = AsyncMock(side_effect=add)
        with self.assertRaises(RuleError):
            await apply_roles(
                guild,
                member.id,
                {1, 2},
                2,
                "respin",
                opt_in_role_id=3,
                respin_role_id=4,
                mark_respun=True,
            )
        self.assertEqual({r.id for r in member.roles}, {2, 3})
        member.add_roles = original
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            2,
            "retry",
            opt_in_role_id=3,
            respin_role_id=4,
            mark_respun=True,
        )
        self.assertEqual({r.id for r in member.roles}, {2, 4})

    async def test_clear_one_country_preserves_other_country_and_club_roles(self):
        guild, member, events = self.fixture([1, 2, 3])
        await apply_roles(guild, member.id, {1, 2}, 2, "clear", clear_country_ids=[1])
        self.assertEqual({r.id for r in member.roles}, {2, 3})
        self.assertEqual(events, [("remove", 1)])

    async def test_clear_all_countries_removes_marker_only_when_requested(self):
        guild, member, events = self.fixture([1, 2, 3, 4])
        await apply_roles(
            guild,
            member.id,
            {1, 2},
            None,
            "clear",
            clear_country_ids=[1, 2],
            respin_role_id=4,
            clear_respun=True,
        )
        self.assertEqual({r.id for r in member.roles}, {3})

    async def test_clear_is_idempotent(self):
        guild, member, events = self.fixture([2, 3])
        await apply_roles(guild, member.id, {1, 2}, 2, "clear", clear_country_ids=[1])
        self.assertEqual(events, [])
