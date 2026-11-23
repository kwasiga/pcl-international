import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.engine import Engine
from bot.services.rules import choose_country
from test_postgres import Guild, Role


class GoalkeeperDraftRulesTests(unittest.TestCase):
    def test_gk_prefers_uncovered_country_even_when_it_has_more_players(self):
        self.assertEqual(
            choose_country(
                [1, 2], {1: 5, 2: 8}, {}, None, 5, goalkeeper=True, goalkeeper_counts={1: 1}
            ),
            2,
        )

    def test_size_balancing_still_applies_among_uncovered_countries(self):
        self.assertEqual(
            choose_country(
                [1, 2, 3],
                {1: 2, 2: 7, 3: 5},
                {},
                None,
                5,
                goalkeeper=True,
                goalkeeper_counts={1: 1},
            ),
            3,
        )

    def test_random_ties_include_all_equally_small_uncovered_countries(self):
        def choose(countries):
            self.assertEqual(countries, [2, 3])
            return 3

        self.assertEqual(
            choose_country(
                [1, 2, 3],
                {1: 4, 2: 5, 3: 5},
                {},
                None,
                5,
                goalkeeper=True,
                goalkeeper_counts={1: 1},
                chooser=choose,
            ),
            3,
        )

    def test_full_country_without_gk_is_still_ineligible(self):
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 16, 2: 17},
                {},
                None,
                5,
                goalkeeper=True,
                goalkeeper_counts={1: 1},
                country_cap=17,
            ),
            1,
        )

    def test_club_limit_is_not_bypassed_to_fill_a_gk_vacancy(self):
        self.assertEqual(
            choose_country(
                [1, 2], {1: 16, 2: 10}, {(2, 8): 5}, 8, 5, goalkeeper=True, goalkeeper_counts={1: 1}
            ),
            1,
        )

    def test_no_eligible_capacity_leaves_gk_unassigned(self):
        self.assertIsNone(
            choose_country([1, 2], {1: 17, 2: 17}, {}, None, 5, goalkeeper=True, country_cap=17)
        )

    def test_outfield_players_keep_existing_size_balancing(self):
        self.assertEqual(
            choose_country([1, 2], {1: 5, 2: 8}, {}, None, 5, goalkeeper_counts={1: 1}), 1
        )

    def test_all_countries_covered_falls_back_to_existing_size_balancing(self):
        self.assertEqual(
            choose_country(
                [1, 2], {1: 5, 2: 8}, {}, None, 5, goalkeeper=True, goalkeeper_counts={1: 3, 2: 1}
            ),
            1,
        )

    def test_gk_respins_remain_random_across_all_eligible_countries(self):
        def choose(countries):
            self.assertEqual(countries, [1, 2])
            return 1

        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 5, 2: 8},
                {},
                None,
                5,
                goalkeeper=True,
                goalkeeper_counts={1: 1},
                balance=False,
                chooser=choose,
            ),
            1,
        )


class GoalkeeperDraftEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_coverage_counts_actual_roles_saved_and_reserved_places_once(self):
        guild = Guild()
        guild.role_map[30] = Role(30)
        guild.player(100, (1, 30))
        guild.player(101, (2, 30))  # Actual country role, not in the database rows.
        guild.player(102, (30,))  # Pending waitlist reservation.
        guild.player(103, (30,))  # Unfinished role operation.
        guild.player(104, (3, 30))  # Incoming GK must not count themselves.
        guild.player(105, (1,))  # Non-GK.
        guild.player(106, (3, 30)).bot = True
        con = SimpleNamespace(
            fetch=AsyncMock(
                return_value=[
                    {"user_id": 100, "country": 1},
                    {"user_id": 100, "country": 1},
                    {"user_id": 102, "country": 3},
                    {"user_id": 103, "country": 3},
                    {"user_id": 104, "country": 3},
                    {"user_id": 105, "country": 1},
                    {"user_id": 106, "country": 3},
                    {"user_id": 999, "country": 2},
                ]
            )
        )
        result = await Engine(None, None).goalkeeper_counts(con, guild, 30, {1, 2, 3}, 104)
        self.assertEqual(result, {1: 1, 2: 1, 3: 2})
        self.assertEqual(con.fetch.call_args.args[1:], (guild.id, [100, 101, 102, 103]))
        sql = con.fetch.call_args.args[0]
        self.assertIn("reservation_active", sql)
        self.assertIn("status IN ('pending','failed')", sql)

    async def test_no_other_present_gks_means_zero_coverage_without_database_query(self):
        guild = Guild()
        guild.player(100, (3,))
        con = SimpleNamespace(fetch=AsyncMock())
        self.assertEqual(await Engine(None, None).goalkeeper_counts(con, guild, 3, {1, 2}, 100), {})
        con.fetch.assert_not_awaited()

    async def test_available_uses_configured_gk_role_only_for_balanced_automatic_choices(self):
        guild = Guild()
        member = guild.player(100, (3, 8))
        con = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"club_conflict": False, "primary_club_role_id": 8})
        )
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"international_gk_role_id": 3, "club_limit": 5})
        engine.counts = AsyncMock(return_value=({1: 5, 2: 8}, {}))
        engine.role_ids = AsyncMock(return_value={1, 2})
        engine.goalkeeper_counts = AsyncMock(return_value={1: 1})
        self.assertEqual(await engine.available(con, guild, member.id), 2)
        engine.goalkeeper_counts.assert_awaited_once_with(con, guild, 3, {1, 2}, member.id)
        engine.goalkeeper_counts.reset_mock()
        with patch("bot.services.rules.secrets.choice", side_effect=min):
            self.assertEqual(await engine.available(con, guild, member.id, balance=False), 1)
        engine.goalkeeper_counts.assert_not_awaited()
        member.roles = [guild.get_role(8)]
        self.assertEqual(await engine.available(con, guild, member.id), 1)
        engine.goalkeeper_counts.assert_not_awaited()

    async def test_draft_reserves_gks_first_and_preserves_registration_order_within_groups(self):
        guild = Guild()
        for uid, roles in ((100, (8,)), (101, (3,)), (102, (8,)), (103, (3,))):
            guild.player(uid, roles)
        rows = [{"user_id": uid, "registration_source": "opt_in"} for uid in (100, 101, 102, 103)]
        con = SimpleNamespace(fetch=AsyncMock(return_value=rows), execute=AsyncMock())
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"international_gk_role_id": 3})
        engine.enqueue = AsyncMock()
        await engine.reconcile_waitlist(con, guild, prioritize_goalkeepers=True)
        self.assertEqual(
            [c.args[1].id for c in engine.enqueue.call_args_list], [101, 103, 100, 102]
        )

    async def test_no_configured_gk_role_preserves_registration_order(self):
        guild = Guild()
        for uid in (100, 101):
            guild.player(uid, (3,))
        con = SimpleNamespace(
            fetch=AsyncMock(
                return_value=[
                    {"user_id": uid, "registration_source": "opt_in"} for uid in (100, 101)
                ]
            ),
            execute=AsyncMock(),
        )
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"international_gk_role_id": None})
        engine.enqueue = AsyncMock()
        await engine.reconcile_waitlist(con, guild, prioritize_goalkeepers=True)
        self.assertEqual([c.args[1].id for c in engine.enqueue.call_args_list], [100, 101])
