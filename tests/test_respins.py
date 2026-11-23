import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.engine import Engine
from bot.services.rules import choose_country
from test_postgres import Guild


class RandomRespinTests(unittest.TestCase):
    def test_draw_can_choose_a_larger_country_instead_of_recent_vacancy(self):
        seen = []

        def choose(countries):
            seen.extend(countries)
            return 3

        self.assertEqual(
            choose_country(
                [1, 2, 3], {1: 20, 2: 18, 3: 20}, {}, None, 5, balance=False, chooser=choose
            ),
            3,
        )
        self.assertEqual(seen, [1, 2, 3])

    def test_random_draw_keeps_caps_and_exclusions(self):
        def choose(countries):
            self.assertEqual(countries, [5, 6])
            return 6

        self.assertEqual(
            choose_country(
                [1, 2, 3, 4, 5, 6],
                {3: 20, 5: 10, 6: 18},
                {(4, 8): 5},
                8,
                5,
                excluded=[1, 2],
                country_cap=20,
                balance=False,
                chooser=choose,
            ),
            6,
        )

    def test_single_eligible_country_is_the_only_result(self):
        self.assertEqual(
            choose_country([1, 2], {2: 10}, {}, None, 5, excluded=[1], balance=False), 2
        )

    def test_no_eligible_country_returns_none_without_drawing(self):
        with patch("bot.services.rules.secrets.choice") as choose:
            self.assertIsNone(
                choose_country([1, 2], {2: 20}, {}, None, 5, [1], country_cap=20, balance=False)
            )
        choose.assert_not_called()


class RespinEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_respin_passes_all_eligible_countries_to_secure_random_draw(self):
        guild = Guild()
        member = guild.player(100, (1, 8))
        con = SimpleNamespace(
            fetchval=AsyncMock(return_value=1),
            fetch=AsyncMock(return_value=[{"country_role_id": 1}]),
            fetchrow=AsyncMock(
                return_value={
                    "club_conflict": False,
                    "primary_club_role_id": 8,
                }
            ),
        )
        engine = Engine(None, None)
        engine.config = AsyncMock(return_value={"club_limit": 5, "country_cap": 20})
        engine.cycle = AsyncMock(return_value=10)
        engine.role_ids = AsyncMock(return_value={1, 2, 3})
        engine.counts = AsyncMock(return_value=({1: 10, 2: 8, 3: 10}, {}))

        def choose(countries):
            self.assertEqual(set(countries), {2, 3})
            return 3

        with patch("bot.services.rules.secrets.choice", side_effect=choose):
            self.assertEqual(await engine.respin_target(con, member), 3)
