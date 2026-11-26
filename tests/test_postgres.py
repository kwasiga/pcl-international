"""Integration tests use an isolated schema in TEST_DATABASE_URL, never DATABASE_URL."""

import asyncio
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.database.connection import Database
from bot.services.engine import Engine
from bot.services.rules import BotPausedError, PlayerBannedError, RuleError


class Role:
    def __init__(self, rid, *, managed=False):
        self.id, self.name, self.managed = rid, f"Role {rid}", managed
        self.permissions = SimpleNamespace(value=0)

    def __ge__(self, other):
        return self.id >= other.id

    def is_default(self):
        return False


class Member:
    def __init__(self, guild, uid, roles=()):
        self.guild, self.id, self.bot = guild, uid, False
        self.roles = [guild.get_role(r) for r in roles]
        self.top_role = Role(10)
        self.fail = False

    async def add_roles(self, role, **kwargs):
        if self.fail:
            raise RuleError("simulated Discord failure")
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, **kwargs):
        self.roles = [r for r in self.roles if r.id != role.id]


class Guild:
    id, owner_id, chunked = 50001, 999, True

    def __init__(self):
        self.role_map = {r: Role(r) for r in (1, 2, 3, 8, 9, 20, 21)}
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True), top_role=Role(100)
        )
        self.members = []
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123456)))

    @property
    def roles(self):
        return list(self.role_map.values())

    async def create_role(self, **kwargs):
        role = Role(30)
        role.name = kwargs["name"]
        self.role_map[role.id] = role
        return role

    def get_role(self, rid):
        return self.role_map.get(rid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return self.channel

    async def fetch_member(self, uid):
        member = self.get_member(uid)
        if member is None:
            raise RuleError("member absent")
        return member

    def player(self, uid, roles=(8,)):
        member = Member(self, uid, roles)
        self.members.append(member)
        return member


@unittest.skipUnless(
    os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run PostgreSQL integration tests"
)
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg

        self.schema = "pcl_test_" + uuid.uuid4().hex
        self.admin = await asyncpg.connect(os.environ["TEST_DATABASE_URL"], statement_cache_size=0)
        await self.admin.execute(f"CREATE SCHEMA {self.schema}")

        async def setup(con):
            await con.execute(f"SET search_path TO {self.schema}")

        pool = await asyncpg.create_pool(
            os.environ["TEST_DATABASE_URL"],
            min_size=1,
            max_size=5,
            setup=setup,
            statement_cache_size=0,
        )
        self.db = Database(pool)
        await self.db.migrate()
        self.guild = Guild()
        self.engine = Engine(self.db, SimpleNamespace(fetch_channel=AsyncMock()))
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "INSERT INTO guild_config(guild_id,staff_role_id,opt_in_role_id,callups_channel_id,logs_channel_id,club_limit) VALUES($1,20,21,30,31,2)",
                self.guild.id,
            )
            await con.execute("INSERT INTO draft_cycles(guild_id) VALUES($1)", self.guild.id)
            for rid in (1, 2):
                await con.execute(
                    "INSERT INTO country_roles(guild_id,role_id) VALUES($1,$2)", self.guild.id, rid
                )
            for rid in (8, 9):
                await con.execute(
                    "INSERT INTO club_roles(guild_id,role_id) VALUES($1,$2)", self.guild.id, rid
                )

    async def asyncTearDown(self):
        await self.db.close()
        await self.admin.execute(f"DROP SCHEMA {self.schema} CASCADE")
        await self.admin.close()

    async def fetchval(self, sql, *args):
        async with self.db.pool.acquire() as con:
            return await con.fetchval(sql, *args)

    async def test_concurrent_optins_reserve_capacity_and_do_not_duplicate(self):
        members = [self.guild.player(uid) for uid in range(100, 110)]
        await asyncio.gather(*(self.engine.register(m) for m in members))
        async with self.db.pool.acquire() as con:
            pop, clubs = await self.engine.counts(con, self.guild.id)
        self.assertEqual(sum(pop.values()), 4)
        self.assertTrue(all(n <= 2 for n in clubs.values()))
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='unassigned'"),
            6,
        )
        results = await asyncio.gather(
            self.engine.register(members[0]),
            self.engine.register(members[0]),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(r, RuleError) for r in results))

    async def opt_in(self, member):
        before = SimpleNamespace(roles=[role for role in member.roles if role.id != 21])
        return await self.engine.member_update(before, member)

    async def test_optin_immediately_assigns_reserved_country_once_and_announces(self):
        member = self.guild.player(100, (8, 21))
        self.assertTrue(await self.opt_in(member))
        country = await self.fetchval(
            "SELECT current_country_role_id FROM players WHERE user_id=100"
        )
        self.assertIn(self.guild.get_role(country), member.roles)
        self.assertNotIn(self.guild.get_role(21), member.roles)
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=100"),
            "completed",
        )
        registration = await self.fetchval(
            "SELECT details->>'country' FROM audit_logs WHERE action='waitlist registration' AND target_user_id=100"
        )
        self.assertEqual(int(registration), country)
        await self.opt_in(member)
        await self.engine.process_auto_drafts(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE user_id=100"), 1
        )
        await self.engine.flush_outbox(self.guild)
        announcements = [
            call.args[0]
            for call in self.guild.channel.send.call_args_list
            if "has been assigned to" in call.args[0]
        ]
        self.assertEqual(announcements, [f"**<@100> has been assigned to <@&{country}>!**"])
        self.assertEqual(await self.engine.release_country(self.guild, 77, country), (country, 1))
        with self.assertRaisesRegex(RuleError, "unreleased"):
            await self.engine.release_country(self.guild, 77, country)

    async def test_concurrent_automatic_optins_respect_country_and_club_caps(self):
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=2 WHERE guild_id=$1", self.guild.id
            )
        members = [self.guild.player(uid, (8, 21)) for uid in range(100, 110)]
        await asyncio.gather(*(self.opt_in(member) for member in members))
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 4
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='unassigned'"),
            6,
        )
        async with self.db.pool.acquire() as con:
            population, clubs = await self.engine.counts(con, self.guild.id, guild=self.guild)
        self.assertTrue(all(count <= 2 for count in population.values()))
        self.assertTrue(all(count <= 2 for count in clubs.values()))

    async def test_draft_pause_queues_optin_then_resumes_the_same_reservation(self):
        self.guild.player(77, (20,))
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, True, feature="draft")
        member = self.guild.player(100, (8, 21))
        self.assertFalse(await self.opt_in(member))
        country = await self.fetchval(
            "SELECT proposed_country_role_id FROM waitlist_entries WHERE user_id=100"
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignments"), 0)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, False, feature="draft")
        await self.engine.process_auto_drafts(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100"),
            country,
        )
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM audit_logs WHERE action='waitlist registration'"
            ),
            1,
        )

    async def test_failed_automatic_role_change_recovers_without_another_draft(self):
        member = self.guild.player(100, (8, 21))
        member.fail = True
        self.assertFalse(await self.opt_in(member))
        self.assertEqual(
            await self.fetchval("SELECT status FROM role_operations WHERE user_id=100"), "failed"
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 0)
        member.fail = False
        await self.engine.recover(self.guild)
        await self.engine.process_auto_drafts(self.guild)
        await self.engine.recover(self.guild)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM announcement_outbox WHERE dedupe_key LIKE 'operation:%'"
            ),
            1,
        )

    async def test_waiting_optin_is_assigned_when_a_full_country_gets_space(self):
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=1 WHERE guild_id=$1", self.guild.id
            )
        first, second = self.guild.player(100), self.guild.player(101)
        await self.engine.assign(first, 1, 77)
        await self.engine.assign(second, 2, 77)
        incoming = self.guild.player(102, (8, 21))
        await self.opt_in(incoming)
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=102"),
            "unassigned",
        )
        await self.engine.assign(first, None, 77, action="remove")
        await self.engine.process_auto_drafts(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=102"), 1
        )

    async def test_automatic_gk_optin_prefers_country_without_keeper_and_bans_still_block(self):
        self.guild.role_map[30] = Role(30)
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET international_gk_role_id=30 WHERE guild_id=$1",
                self.guild.id,
            )
        self.guild.player(100, (1, 30))
        incoming = self.guild.player(101, (30, 21))
        self.assertTrue(await self.opt_in(incoming))
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=101"), 2
        )
        banned = self.guild.player(102, (8,))
        await self.engine.ban_player(banned, 77)
        banned.roles.append(self.guild.get_role(21))
        self.assertFalse(await self.opt_in(banned))
        self.assertIsNone(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=102")
        )

    async def test_ban_removes_assignment_and_marker_preserving_history_and_respin_usage(self):
        member = self.guild.player(100, (8, 21))
        await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 2, 77, action="respin")
        member.roles.append(self.guild.get_role(21))
        self.assertTrue(await self.engine.ban_player(member, 77, "Inactive"))
        self.assertEqual({role.id for role in member.roles}, {8})
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 1)
        self.assertIsNone(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100")
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 0
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 3)
        async with self.db.pool.acquire() as con:
            self.assertEqual(
                await self.engine.respins_used(
                    con, self.guild.id, 100, await self.engine.cycle(con, self.guild.id)
                ),
                1,
            )
        for action in (
            lambda: self.engine.register(member),
            lambda: self.engine.propose_respin(member, override_limit=True),
            lambda: self.engine.assign(member, 1, 77, override=True),
        ):
            with self.assertRaises(PlayerBannedError):
                await action()
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 3)

    async def test_banning_waitlisted_player_releases_reservation_and_unban_allows_new_registration(
        self,
    ):
        member = self.guild.player(100, (21,))
        await self.engine.register(member)
        self.assertTrue(await self.engine.ban_player(member, 77))
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=100"), "removed"
        )
        self.assertFalse(
            await self.fetchval("SELECT reservation_active FROM waitlist_entries WHERE user_id=100")
        )
        self.assertEqual(member.roles, [])
        await self.engine.unban_player(self.guild, 100, 77)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 0)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignments"), 0)
        self.assertEqual(await self.engine.register(member), "pending")
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='pending'"), 1
        )

    async def test_ban_stays_effective_after_role_failure_and_recovers_once(self):
        member = self.guild.player(100, (8,))
        await self.engine.assign(member, 1, 77)
        remove_roles = member.remove_roles
        member.remove_roles = AsyncMock(side_effect=RuleError("Missing Manage Roles"))
        self.assertFalse(await self.engine.ban_player(member, 77))
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 1)
        with self.assertRaisesRegex(RuleError, "unfinished role operation"):
            await self.engine.unban_player(self.guild, 100, 77)
        with self.assertRaises(PlayerBannedError):
            await self.engine.register(member)
        member.remove_roles = remove_roles
        restarted = Engine(self.db, self.engine.bot)
        await restarted.recover(self.guild)
        await restarted.recover(self.guild)
        self.assertEqual({role.id for role in member.roles}, {8})
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE action='ban'"), 1
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 1)
        self.guild.members.remove(member)
        await restarted.unban_player(self.guild, 100, 77)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 0)

    async def test_bans_survive_reset_and_rejoin_without_readding_inactive_players(self):
        member = self.guild.player(100, (8, 21))
        await self.engine.ban_player(member, 77)
        member.roles.append(self.guild.get_role(21))
        eligible = self.guild.player(101, (21,))
        self.assertTrue(await self.engine.start_reset(self.guild, 77))
        self.assertEqual(await self.fetchval("SELECT count(*) FROM player_bans"), 1)
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM waitlist_entries WHERE user_id=100 AND status='pending'"
            ),
            0,
        )
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM waitlist_entries WHERE user_id=101 AND status='pending'"
            ),
            1,
        )
        await self.engine.member_joined(member)
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM role_operations WHERE user_id=100 AND action='restore'"
            ),
            0,
        )
        self.assertIn(self.guild.get_role(21), eligible.roles)

    async def test_pause_persists_across_restart_and_holds_outbox_until_resume(self):
        member = self.guild.player(100)
        self.guild.player(77, (20,))
        await self.engine.assign(member, 1, 77)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            self.assertTrue(await self.engine.set_paused(self.guild, 77, True))
            self.assertFalse(await self.engine.set_paused(self.guild, 77, True))
        restarted = Engine(self.db, self.engine.bot)
        with self.assertRaises(BotPausedError):
            await restarted.register(self.guild.player(101))
        await restarted.recover(self.guild)
        await restarted.flush_outbox(self.guild)
        self.guild.channel.send.assert_not_awaited()
        self.assertTrue(await self.fetchval("SELECT paused FROM guild_config"))
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            self.assertTrue(await restarted.set_paused(self.guild, 77, False))
        await restarted.flush_outbox(self.guild)
        self.assertFalse(await self.fetchval("SELECT paused FROM guild_config"))
        messages = [c.args[0] for c in self.guild.channel.send.call_args_list]
        self.assertEqual(sum("has been assigned to" in m for m in messages), 1)

    async def test_feature_pauses_persist_and_resuming_one_keeps_other_pauses(self):
        self.guild.player(77, (20,))
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            self.assertTrue(await self.engine.set_paused(self.guild, 77, True, feature="respins"))
            self.assertFalse(await self.engine.set_paused(self.guild, 77, True, feature="respins"))
            self.assertTrue(await self.engine.set_paused(self.guild, 77, True, feature="draft"))
        restarted = Engine(self.db, self.engine.bot)
        with self.assertRaises(BotPausedError):
            await restarted.propose_respin(self.guild.player(100))
        with self.assertRaises(BotPausedError):
            await restarted.draft(self.guild, 77)
        await restarted.assign(self.guild.player(101), 1, 77)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await restarted.set_paused(self.guild, 77, False, feature="respins")
            self.assertEqual(
                await self.fetchval("SELECT paused_features FROM guild_config"), ["draft"]
            )
            await restarted.set_paused(self.guild, 77, True)
            await restarted.set_paused(self.guild, 77, False, feature="draft")
            self.assertTrue(await self.fetchval("SELECT paused FROM guild_config"))
            await restarted.set_paused(self.guild, 77, True, feature="respins")
            await restarted.set_paused(self.guild, 77, False)
        self.assertFalse(await self.fetchval("SELECT paused FROM guild_config"))
        self.assertEqual(await self.fetchval("SELECT paused_features FROM guild_config"), [])

    async def test_paused_respin_recovery_waits_while_manual_assignments_continue(self):
        player = self.guild.player(100)
        self.guild.player(77, (20,))
        await self.engine.assign(player, 1, 77)
        player.fail = True
        self.assertFalse(await self.engine.assign(player, 2, 77, action="respin"))
        player.fail = False
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, True, feature="respins")
        restarted = Engine(self.db, self.engine.bot)
        await restarted.recover(self.guild)
        await restarted.assign(self.guild.player(101), 2, 77)
        self.assertEqual(
            await self.fetchval("SELECT status FROM role_operations WHERE action='respin'"),
            "failed",
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE action='respin'"), 0
        )
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await restarted.set_paused(self.guild, 77, False, feature="respins")
        await restarted.recover(self.guild)
        await restarted.recover(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT status FROM role_operations WHERE action='respin'"),
            "completed",
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE action='respin'"), 1
        )

    async def test_announcements_can_pause_while_assignments_continue(self):
        self.guild.player(77, (20,))
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, True, feature="announcements")
        await self.engine.assign(self.guild.player(100), 1, 77)
        await self.engine.flush_outbox(self.guild)
        self.guild.channel.send.assert_not_awaited()
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 1
        )
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, False, feature="announcements")
        await self.engine.flush_outbox(self.guild)
        self.assertEqual(
            sum(
                "has been assigned to" in call.args[0]
                for call in self.guild.channel.send.call_args_list
            ),
            1,
        )

    async def test_running_draft_stops_between_players_and_recovers_after_resume(self):
        self.guild.player(77, (20,))
        for uid in (100, 101, 102):
            await self.engine.register(self.guild.player(uid))
        original = self.engine.execute_operation
        completed = 0

        async def execute(con, guild, opid):
            nonlocal completed
            result = await original(con, guild, opid)
            completed += 1
            if completed == 1:
                with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
                    await self.engine.set_paused(guild, 77, True)
            return result

        self.engine.execute_operation = execute
        with self.assertRaises(BotPausedError):
            await asyncio.wait_for(self.engine.draft(self.guild, 77), timeout=10)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 1
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM role_operations WHERE status='pending'"), 2
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM role_operations WHERE status='failed'"), 0
        )
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, False)
        await self.engine.recover(self.guild)
        await self.engine.recover(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 3
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 3)
        self.assertEqual(await self.fetchval("SELECT status FROM draft_batches"), "completed")

    async def test_member_events_received_while_paused_replay_on_resume(self):
        returning = self.guild.player(100)
        self.guild.player(77, (20,))
        await self.engine.assign(returning, 1, 77)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await self.engine.set_paused(self.guild, 77, True)
        await self.engine.member_left(returning)
        returning.roles = [self.guild.get_role(8)]
        await self.engine.member_joined(returning)
        opted = self.guild.player(101, (8, 21))
        await self.engine.member_update(SimpleNamespace(roles=[self.guild.get_role(8)]), opted)
        self.assertNotIn(self.guild.get_role(1), returning.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM paused_member_events"), 2)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM players WHERE user_id=101"), 0)
        restarted = Engine(self.db, self.engine.bot)
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock):
            await restarted.set_paused(self.guild, 77, False)
        await restarted.recover(self.guild)
        self.assertIn(self.guild.get_role(1), returning.roles)
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=101"),
            "completed",
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM paused_member_events"), 0)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE user_id=100"), 1
        )

    async def test_manual_registration_without_optin_and_multiclub_block(self):
        self.assertEqual(await self.engine.register(self.guild.player(100, ())), "pending")
        self.assertEqual(await self.engine.register(self.guild.player(101, (8, 9))), "blocked")
        await self.engine.set_club(self.guild.get_member(101), self.guild.get_role(9), 77)
        self.assertEqual(
            await self.fetchval("SELECT primary_club_role_id FROM players WHERE user_id=101"), 9
        )
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=101"), "pending"
        )

    async def test_draft_partial_failure_recovers_and_outbox_dedupes(self):
        good, failed = self.guild.player(100), self.guild.player(101)
        await self.engine.register(good)
        await self.engine.register(failed)
        failed.fail = True
        self.assertEqual(await self.engine.draft(self.guild, 77), (1, 1))
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 1
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM role_operations WHERE status='failed'"), 1
        )
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM announcement_outbox WHERE dedupe_key LIKE 'operation:%'"
            ),
            1,
        )
        failed.fail = False
        await self.engine.recover(self.guild)
        await self.engine.recover(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 2
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 2)
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM announcement_outbox WHERE dedupe_key LIKE 'batch:%'"
            ),
            0,
        )
        first, _ = await self.engine.release_country(self.guild, 77)
        second, _ = await self.engine.release_country(self.guild, 77)
        self.assertNotEqual(first, second)
        with self.assertRaises(RuleError):
            await self.engine.release_country(self.guild, 77)
        await self.engine.flush_outbox(self.guild)
        sent = self.guild.channel.send.await_count
        await self.engine.flush_outbox(self.guild)
        self.assertEqual(self.guild.channel.send.await_count, sent)
        messages = [
            c.args[0]
            for c in self.guild.channel.send.call_args_list
            if "OFFICIAL CALL-UPS" in c.args[0]
        ]
        self.assertEqual(sum(m.count("<@100>") for m in messages), 1)
        self.assertEqual(sum(m.count("<@101>") for m in messages), 1)
        assignments = [
            c.args[0]
            for c in self.guild.channel.send.call_args_list
            if "has been assigned to" in c.args[0]
        ]
        self.assertEqual(len(assignments), 2)
        self.assertEqual(sum(m.count("<@100>") for m in assignments), 1)
        self.assertEqual(sum(m.count("<@101>") for m in assignments), 1)

    async def test_crash_after_discord_before_database_recovers(self):
        member = self.guild.player(100)
        await self.engine.register(member)
        async with self.db.guild(self.guild.id) as con:
            async with con.transaction():
                opid = await self.engine.prepare_operation(con, self.guild, 100, 1, "manual", 77)
        await member.add_roles(self.guild.get_role(1))
        await self.engine.recover(self.guild)
        await self.engine.recover(self.guild)
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100"), 1
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)
        self.assertEqual(
            await self.fetchval("SELECT status FROM role_operations WHERE id=$1", opid), "completed"
        )

    async def test_country_release_waits_for_full_squad_and_is_serialized(self):
        async with self.db.pool.acquire() as con:
            await con.execute("UPDATE country_roles SET enabled=FALSE WHERE role_id=2")
        good, failed = self.guild.player(100), self.guild.player(101)
        await self.engine.register(good)
        await self.engine.register(failed)
        failed.fail = True
        self.assertEqual(await self.engine.draft(self.guild, 77), (1, 1))
        with self.assertRaises(RuleError):
            await self.engine.release_country(self.guild, 77, 1)
        failed.fail = False
        await self.engine.recover(self.guild)
        results = await asyncio.gather(
            self.engine.release_country(self.guild, 77, 1),
            self.engine.release_country(self.guild, 77, 1),
            return_exceptions=True,
        )
        self.assertEqual(results.count((1, 2)), 1)
        self.assertEqual(sum(isinstance(r, RuleError) for r in results), 1)
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM announcement_outbox WHERE dedupe_key LIKE 'batch:country:%'"
            ),
            1,
        )

    async def test_release_uses_current_roster_and_does_not_replay_old_announcements(self):
        for uid in (100, 101, 102, 103):
            await self.engine.register(self.guild.player(uid, ()))
        self.assertEqual(await self.engine.draft(self.guild, 77), (4, 0))
        selected, count = await self.engine.release_country(self.guild, 77, 1)
        self.assertEqual((selected, count), (1, 2))
        # A process restart still respects durable release markers.
        restarted = Engine(self.db, self.engine.bot)
        with self.assertRaises(RuleError):
            await restarted.release_country(self.guild, 77, 1)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 4)
        async with self.db.pool.acquire() as con:
            member = await con.fetchval(
                "SELECT user_id FROM players WHERE current_country_role_id=2 LIMIT 1"
            )
        await self.engine.assign(self.guild.get_member(member), None, 77, action="remove")
        self.assertEqual(await restarted.release_country(self.guild, 77, 2), (2, 1))

    async def test_respin_history_exception_and_remove_clear_exclusions(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        current, target, exceptions, _, _ = await self.engine.propose_respin(member)
        self.assertEqual((current, target), (1, 2))
        await self.engine.assign(member, 2, 77, expected=1, action="respin")
        current, target, exceptions, _, _ = await self.engine.propose_respin(member)
        self.assertIsNone(target)
        self.assertEqual(exceptions, [1])
        await self.engine.assign(member, 1, 77, expected=2, action="respin_exception")
        await self.engine.assign(member, None, 77, action="remove")
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 4)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM cycle_exclusions"), 0)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='pending'"), 0
        )

    async def test_manual_override_is_explicit_and_logged(self):
        for uid in (100, 101):
            await self.engine.assign(self.guild.player(uid), 1, 77)
        member = self.guild.player(102)
        self.assertTrue(await self.engine.needs_override(member, 1))
        with self.assertRaises(RuleError):
            await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 1, 77, override=True)
        self.assertEqual(
            await self.fetchval("SELECT override_type FROM assignment_history WHERE user_id=102"),
            "club_capacity",
        )

    async def test_reroll_limit_counts_recovery_once_and_blocks_a_fourth(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 2, 77, action="respin")
        await self.engine.assign(member, 1, 77, action="respin_exception")
        current, target, exceptions, cycle, used = await self.engine.propose_respin(member)
        self.assertEqual((current, target, exceptions, used), (1, None, [2], 2))
        member.fail = True
        self.assertFalse(
            await self.engine.assign(
                member, 2, 77, action="respin_exception", expected_respins=2, expected_cycle=cycle
            )
        )
        async with self.db.pool.acquire() as con:
            self.assertEqual(await self.engine.respins_used(con, self.guild.id, 100, cycle), 2)
        member.fail = False
        await self.engine.recover(self.guild)
        await self.engine.recover(self.guild)
        async with self.db.pool.acquire() as con:
            self.assertEqual(await self.engine.respins_used(con, self.guild.id, 100, cycle), 3)
        with self.assertRaisesRegex(RuleError, "no rerolls left"):
            await self.engine.propose_respin(member)
        with self.assertRaisesRegex(RuleError, "no rerolls left"):
            await self.engine.assign(member, 1, 77, action="respin_exception")

    async def test_reroll_override_is_logged_remove_preserves_usage_and_reset_replenishes(self):
        member = self.guild.player(100)
        self.guild.player(77, (20,))
        await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 2, 77, action="respin")
        await self.engine.assign(member, 1, 77, action="respin_exception")
        await self.engine.assign(member, 2, 77, action="respin_exception")
        with patch("bot.services.engine.require_staff", new_callable=AsyncMock) as admin:
            await self.engine.assign(
                member,
                1,
                77,
                action="respin_exception",
                respin_override=True,
                expected_respins=3,
            )
        admin.assert_awaited_once()
        self.assertEqual(
            await self.fetchval(
                "SELECT override_type FROM assignment_history ORDER BY id DESC LIMIT 1"
            ),
            "respin_limit",
        )
        async with self.db.pool.acquire() as con:
            old_cycle = await self.engine.cycle(con, self.guild.id)
            self.assertEqual(await self.engine.respins_used(con, self.guild.id, 100, old_cycle), 4)
        await self.engine.assign(member, None, 77, action="remove")
        await self.engine.assign(member, 1, 77)
        with self.assertRaisesRegex(RuleError, "no rerolls left"):
            await self.engine.propose_respin(member)
        self.assertTrue(await self.engine.start_reset(self.guild, 77))
        async with self.db.pool.acquire() as con:
            new_cycle = await self.engine.cycle(con, self.guild.id)
            self.assertNotEqual(new_cycle, old_cycle)
            self.assertEqual(await self.engine.respins_used(con, self.guild.id, 100, new_cycle), 0)
            self.assertEqual(await self.engine.respins_used(con, self.guild.id, 100, old_cycle), 4)

    async def test_disabled_country_never_selected_and_disabled_club_unaffiliated(self):
        async with self.db.guild(self.guild.id) as con:
            await con.execute("UPDATE country_roles SET enabled=FALSE WHERE role_id=2")
            await con.execute("UPDATE club_roles SET enabled=FALSE WHERE role_id=8")
        for uid in range(100, 105):
            await self.engine.register(self.guild.player(uid))
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM players WHERE primary_club_role_id IS NOT NULL"
            ),
            0,
        )
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM waitlist_entries WHERE proposed_country_role_id=1"
            ),
            5,
        )

    async def test_leaving_pending_releases_and_rejoin_reassigns(self):
        member = self.guild.player(100)
        await self.engine.register(member)
        self.guild.members.remove(member)
        await self.engine.member_left(member)
        self.assertFalse(
            await self.fetchval("SELECT reservation_active FROM waitlist_entries WHERE user_id=100")
        )
        self.guild.members.append(member)
        await self.engine.member_joined(member)
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=100"), "pending"
        )

    async def test_return_restoration_is_journalled_without_duplicate_history(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        await self.engine.member_left(member)
        member.roles = [self.guild.get_role(8)]
        await self.engine.member_joined(member)
        self.assertIn(self.guild.get_role(1), member.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)

    async def test_reset_preserves_history_rebuilds_optins_only(self):
        opted = self.guild.player(100, (8, 21))
        manual = self.guild.player(101, (8,))
        await self.engine.assign(opted, 1, 77)
        await self.engine.register(manual)
        # Include an unmanaged DB player holding an extra configured role.
        extra = self.guild.player(102, (2,))
        self.assertTrue(await self.engine.start_reset(self.guild, 77))
        self.assertEqual(
            await self.fetchval(
                "SELECT count(*) FROM players WHERE current_country_role_id IS NOT NULL"
            ),
            0,
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM waitlist_entries WHERE status='pending'"), 0
        )
        self.assertGreaterEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 2)
        self.assertNotIn(self.guild.get_role(2), extra.roles)

    async def test_reset_failure_freezes_then_recovers(self):
        member = self.guild.player(100, (8, 21))
        await self.engine.assign(member, 1, 77)
        original = member.remove_roles
        member.remove_roles = AsyncMock(side_effect=RuleError("cannot remove"))
        self.assertFalse(await self.engine.start_reset(self.guild, 77))
        with self.assertRaises(RuleError):
            await self.engine.register(self.guild.player(101))
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100"), 1
        )
        member.remove_roles = original
        await self.engine.recover(self.guild)
        self.assertFalse(await self.fetchval("SELECT resetting FROM guild_config"))
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM draft_cycles WHERE status='active'"), 1
        )

    async def test_sync_flags_manual_role_removal_without_restoring(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        await member.remove_roles(self.guild.get_role(1))
        self.guild.channel.permissions_for = lambda _: SimpleNamespace(send_messages=True)
        issues = await self.engine.sync(self.guild, 77)
        self.assertTrue(any("<@100>" in issue for issue in issues))
        self.assertNotIn(self.guild.get_role(1), member.roles)

    async def test_respin_marker_and_optin_cleanup_persist(self):
        member = self.guild.player(100, (8, 21))
        await self.engine.assign(member, 1, 77)
        self.assertNotIn(self.guild.get_role(21), member.roles)
        await self.engine.assign(member, 2, 77, action="respin", expected=1)
        marker_id = await self.fetchval("SELECT respin_role_id FROM guild_config")
        self.assertIsNotNone(marker_id)
        self.assertIn(self.guild.get_role(marker_id), member.roles)
        self.assertTrue(await self.fetchval("SELECT has_respun FROM players WHERE user_id=100"))
        await self.engine.assign(member, None, 77, action="remove")
        self.assertNotIn(self.guild.get_role(marker_id), member.roles)
        self.assertFalse(await self.fetchval("SELECT has_respun FROM players WHERE user_id=100"))

    async def test_sync_cleans_existing_optin_without_duplicate_history(self):
        member = self.guild.player(100, (8, 21))
        await self.engine.assign(member, 1, 77)
        await member.add_roles(self.guild.get_role(21))
        self.guild.channel.permissions_for = lambda _: SimpleNamespace(send_messages=True)
        await self.engine.sync(self.guild, 77)
        self.assertNotIn(self.guild.get_role(21), member.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)

    async def test_rejoin_restores_respun_marker(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 2, 77, action="respin", expected=1)
        marker_id = await self.fetchval("SELECT respin_role_id FROM guild_config")
        await self.engine.member_left(member)
        member.roles = [self.guild.get_role(8)]
        await self.engine.member_joined(member)
        self.assertIn(self.guild.get_role(2), member.roles)
        self.assertIn(self.guild.get_role(marker_id), member.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 2)

    async def test_clear_one_country_keeps_other_assignments_and_current_cycle(self):
        first, other = self.guild.player(100), self.guild.player(101)
        await self.engine.assign(first, 1, 77)
        await self.engine.assign(other, 2, 77)
        await other.add_roles(self.guild.get_role(1))
        untracked = self.guild.player(102, (1, 8))
        cycle = await self.fetchval("SELECT id FROM draft_cycles WHERE status='active'")
        self.assertEqual(await self.engine.clear_countries(self.guild, 77, 1), (3, 0))
        self.assertIsNone(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100")
        )
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=101"), 2
        )
        self.assertIn(self.guild.get_role(2), other.roles)
        self.assertNotIn(self.guild.get_role(1), other.roles)
        self.assertNotIn(self.guild.get_role(1), untracked.roles)
        self.assertEqual(
            await self.fetchval("SELECT id FROM draft_cycles WHERE status='active'"), cycle
        )
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignment_history WHERE action='manual'"), 2
        )

    async def test_clear_all_removes_country_and_respun_roles(self):
        member = self.guild.player(100)
        await self.engine.assign(member, 1, 77)
        await self.engine.assign(member, 2, 77, action="respin", expected=1)
        marker_id = await self.fetchval("SELECT respin_role_id FROM guild_config")
        self.assertEqual(await self.engine.clear_countries(self.guild, 77), (1, 0))
        self.assertEqual({r.id for r in member.roles}, {8})
        self.assertNotIn(self.guild.get_role(marker_id), member.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM cycle_exclusions"), 0)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 3)

    async def test_automatic_gk_draft_accounts_for_country_roles_assigned_outside_bot(self):
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET international_gk_role_id=3 WHERE guild_id=$1",
                self.guild.id,
            )
        existing = self.guild.player(200, (1, 3))
        incoming = self.guild.player(100, (3,))
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            await self.engine.register(incoming)
            self.assertEqual(
                await self.fetchval(
                    "SELECT proposed_country_role_id FROM waitlist_entries WHERE user_id=100"
                ),
                2,
            )
            self.assertEqual(await self.engine.draft(self.guild, 77), (1, 0))
        self.assertIn(self.guild.get_role(2), incoming.roles)
        self.assertIn(self.guild.get_role(1), existing.roles)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM players WHERE user_id=200"), 0)

    async def test_pending_gk_reservations_spread_keepers_before_filling_second_gk_places(self):
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET international_gk_role_id=3 WHERE guild_id=$1",
                self.guild.id,
            )
        for uid in (200, 201, 202):
            await self.engine.assign(self.guild.player(uid, ()), 2, 77)
        first, second = self.guild.player(100, (3,)), self.guild.player(101, (3,))
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            await self.engine.register(first)
            await self.engine.register(second)
            self.assertEqual(
                await self.fetchval(
                    "SELECT proposed_country_role_id FROM waitlist_entries WHERE user_id=100"
                ),
                1,
            )
            self.assertEqual(
                await self.fetchval(
                    "SELECT proposed_country_role_id FROM waitlist_entries WHERE user_id=101"
                ),
                2,
            )
            self.assertEqual(await self.engine.draft(self.guild, 77), (2, 0))
        self.assertIn(self.guild.get_role(1), first.roles)
        self.assertIn(self.guild.get_role(2), second.roles)

    async def test_draft_preserves_reservations_and_keeps_later_gk_waiting_when_full(self):
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE guild_config SET international_gk_role_id=3,country_cap=1 WHERE guild_id=$1",
                self.guild.id,
            )
        for uid in (100, 101):
            await self.engine.register(self.guild.player(uid, ()))
        goalkeeper = self.guild.player(102, (3,))
        self.assertEqual(await self.engine.register(goalkeeper), "unassigned")
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            self.assertEqual(await self.engine.draft(self.guild, 77), (2, 0))
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=102"),
            "unassigned",
        )
        self.assertEqual(
            await self.fetchval("SELECT status FROM waitlist_entries WHERE user_id=101"),
            "completed",
        )
        async with self.db.pool.acquire() as con:
            population, _ = await self.engine.counts(con, self.guild.id)
        self.assertEqual(dict(population), {1: 1, 2: 1})

    async def test_legacy_positions_no_longer_affect_draft_choices(self):
        self.guild.role_map[4] = Role(4)
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "INSERT INTO position_roles VALUES($1,3,'GK'),($1,4,'ATT')", self.guild.id
            )
        attacker = self.guild.player(100, (8, 4))
        goalkeeper = self.guild.player(101, (8, 3))
        await self.engine.assign(attacker, 1, 77)
        await self.engine.assign(goalkeeper, 2, 77)
        incoming = self.guild.player(102, (4,))
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            await self.engine.register(incoming)
        self.assertEqual(
            await self.fetchval(
                "SELECT proposed_country_role_id FROM waitlist_entries WHERE user_id=102"
            ),
            1,
        )
        self.assertEqual(
            await self.fetchval("SELECT position_groups FROM players WHERE user_id=102"), []
        )

    async def test_removing_one_club_from_setup_preserves_teams_history_and_cycle(self):
        removed_club_player = self.guild.player(100, (8,))
        other_player = self.guild.player(101, (9,))
        await self.engine.assign(removed_club_player, 1, 77)
        await self.engine.assign(other_player, 2, 77)
        async with self.db.pool.acquire() as con:
            cycle = await self.engine.cycle(con, self.guild.id)
            history_count = await con.fetchval("SELECT count(*) FROM assignment_history")
            exclusions = await con.fetch("SELECT * FROM cycle_exclusions ORDER BY user_id")
        values = dict(
            countries={1, 2},
            clubs={9},
            staff_role_id=20,
            opt_in_role_id=21,
            callups_channel_id=None,
            logs_channel_id=None,
            club_limit=2,
        )
        await self.engine.save_config(self.guild, values, 77, expected_revision=1)
        async with self.db.pool.acquire() as con:
            self.assertEqual(await self.engine.role_ids(con, self.guild.id, "club", True), {9})
            self.assertEqual(
                await self.engine.role_ids(con, self.guild.id, "country", True), {1, 2}
            )
            self.assertEqual(await self.engine.cycle(con, self.guild.id), cycle)
            self.assertEqual(
                await con.fetchval("SELECT count(*) FROM assignment_history"), history_count
            )
            self.assertEqual(
                await con.fetch("SELECT * FROM cycle_exclusions ORDER BY user_id"), exclusions
            )
            players = await con.fetch(
                "SELECT user_id,current_country_role_id,primary_club_role_id FROM players ORDER BY user_id"
            )
        self.assertEqual(
            [tuple(player.values()) for player in players], [(100, 1, None), (101, 2, 9)]
        )
        self.assertEqual({role.id for role in removed_club_player.roles}, {1, 8})
        self.assertEqual({role.id for role in other_player.roles}, {2, 9})

    async def test_international_gk_role_can_be_saved_and_cleared_without_changing_legacy_mappings(
        self,
    ):
        async with self.db.pool.acquire() as con:
            await con.execute("INSERT INTO position_roles VALUES($1,3,'GK')", self.guild.id)
        values = dict(
            countries={1, 2},
            clubs={8, 9},
            staff_role_id=20,
            opt_in_role_id=21,
            callups_channel_id=None,
            logs_channel_id=None,
            club_limit=2,
            international_gk_role_id=3,
        )
        self.guild.get_role(3).managed = True
        await self.engine.save_config(self.guild, values, 77, expected_revision=1)
        self.assertEqual(
            await self.fetchval("SELECT international_gk_role_id FROM guild_config"), 3
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM position_roles"), 1)
        values["international_gk_role_id"] = None
        await self.engine.save_config(self.guild, values, 77, expected_revision=2)
        self.assertIsNone(await self.fetchval("SELECT international_gk_role_id FROM guild_config"))

    async def test_legacy_position_data_is_preserved_but_no_longer_updated(self):
        self.guild.role_map[4] = Role(4)
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "INSERT INTO position_roles VALUES($1,3,'GK'),($1,4,'ATT')", self.guild.id
            )
        member = self.guild.player(100, (3, 4))
        await self.engine.assign(member, 1, 77)
        async with self.db.pool.acquire() as con:
            await con.execute(
                "UPDATE players SET position_groups=ARRAY['ATT','GK'] WHERE user_id=100"
            )
        member.roles = [r for r in member.roles if r.id != 3]
        async with self.db.guild(self.guild.id) as con:
            await self.engine.refresh_player(con, member)
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100"), 1
        )
        self.assertEqual(
            await self.fetchval("SELECT position_groups FROM players WHERE user_id=100"),
            ["ATT", "GK"],
        )

    async def test_country_cap_counts_pending_and_cannot_be_manually_overridden(self):
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=1 WHERE guild_id=$1", self.guild.id
            )
        await self.engine.assign(self.guild.player(100, ()), 1, 77)
        await self.engine.register(self.guild.player(101, ()))
        newcomer = self.guild.player(102, ())
        self.assertEqual(await self.engine.register(newcomer), "unassigned")
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.assign(newcomer, 2, 77, override=True)
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 1
        )

    async def test_concurrent_registrations_respect_country_cap(self):
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=2 WHERE guild_id=$1", self.guild.id
            )
        members = [self.guild.player(uid, ()) for uid in range(100, 120)]
        await asyncio.gather(*(self.engine.register(m) for m in members))
        async with self.db.pool.acquire() as con:
            population, _ = await self.engine.counts(con, self.guild.id)
        self.assertEqual(sum(population.values()), 4)
        self.assertTrue(all(n <= 2 for n in population.values()))

    async def test_untracked_role_holders_block_full_countries_in_respins_and_drafts(self):
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=1 WHERE guild_id=$1", self.guild.id
            )
        player = self.guild.player(100, ())
        await self.engine.assign(player, 1, 77)
        self.guild.player(101, (2,))  # No player row or saved assignment.
        _, target, exceptions, _, _ = await self.engine.propose_respin(player)
        self.assertIsNone(target)
        self.assertEqual(exceptions, [])
        self.assertEqual(await self.engine.register(self.guild.player(102, ())), "unassigned")
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.assign(player, 2, 77, action="respin")
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)

    async def test_country_filling_after_respin_proposal_does_not_consume_reroll(self):
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=1 WHERE guild_id=$1", self.guild.id
            )
        player = self.guild.player(100, ())
        await self.engine.assign(player, 1, 77)
        current, target, _, cycle, used = await self.engine.propose_respin(player)
        self.assertEqual(target, 2)
        self.guild.player(101, (2,))
        with self.assertRaisesRegex(RuleError, "player cap"):
            await self.engine.assign(
                player,
                target,
                77,
                action="respin",
                expected=current,
                expected_cycle=cycle,
                expected_respins=used,
            )
        self.assertEqual(
            await self.fetchval("SELECT current_country_role_id FROM players WHERE user_id=100"), 1
        )
        self.assertEqual(await self.fetchval("SELECT count(*) FROM assignment_history"), 1)
        self.assertEqual(await self.fetchval("SELECT count(*) FROM role_operations"), 1)
        self.assertEqual({role.id for role in player.roles}, {1})

    async def test_lowered_country_cap_preserves_existing_players(self):
        first, second = self.guild.player(100, ()), self.guild.player(101, ())
        await self.engine.assign(first, 1, 77)
        await self.engine.assign(second, 1, 77)
        async with self.db.guild(self.guild.id) as con:
            await con.execute(
                "UPDATE guild_config SET country_cap=1 WHERE guild_id=$1", self.guild.id
            )
            violations = await self.engine.flag_capacity(con, self.guild.id, 77)
        self.assertTrue(any(v.get("country") == 1 for v in violations))
        self.assertEqual(
            await self.fetchval("SELECT count(*) FROM assignments WHERE status='active'"), 2
        )
        self.assertIn(self.guild.get_role(1), first.roles)
        self.assertIn(self.guild.get_role(1), second.roles)
