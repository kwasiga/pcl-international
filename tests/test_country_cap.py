import unittest

from bot.services.rules import choose_country


class CountryCapTests(unittest.TestCase):
    def test_full_country_is_excluded(self):
        self.assertEqual(choose_country([1, 2], {1: 10, 2: 9}, {}, None, 5, country_cap=10), 2)

    def test_all_full_leaves_player_unassigned(self):
        self.assertIsNone(choose_country([1, 2], {1: 10, 2: 10}, {}, None, 5, country_cap=10))

    def test_cap_applies_to_unaffiliated_players(self):
        self.assertIsNone(choose_country([1], {1: 1}, {}, None, 5, country_cap=1))

    def test_omitting_cap_preserves_unlimited_behavior(self):
        self.assertEqual(choose_country([1], {1: 100}, {}, None, 5), 1)

    def test_over_cap_country_never_receives_new_player(self):
        self.assertIsNone(choose_country([1], {1: 12}, {}, None, 5, country_cap=10))

    def test_cap_is_not_overridden_by_position_preferences(self):
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 2, 2: 1},
                {},
                None,
                5,
                player_positions=["GK"],
                position_counts={(2, "GK"): 1},
                country_cap=2,
            ),
            2,
        )
