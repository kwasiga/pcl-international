import unittest

from bot.services.rules import choose_country, player_positions, position_occupancy


class PositionBalanceTests(unittest.TestCase):
    def test_attacker_prefers_equal_size_country_with_fewer_attackers(self):
        counts = {(1, "ATT"): 3, (2, "DEF"): 3}
        self.assertEqual(
            choose_country(
                [1, 2], {1: 3, 2: 3}, {}, None, 5, player_positions=["ATT"], position_counts=counts
            ),
            2,
        )

    def test_goalkeeper_prefers_equal_size_country_without_goalkeeper(self):
        counts = {(1, "GK"): 1, (2, "ATT"): 1}
        self.assertEqual(
            choose_country(
                [1, 2], {1: 1, 2: 1}, {}, None, 5, player_positions=["GK"], position_counts=counts
            ),
            2,
        )

    def test_multirole_member_is_not_counted_twice(self):
        counts = position_occupancy([{"country": 1, "positions": ["DEF", "MID", "MID"]}])
        self.assertEqual(counts[1, "DEF"], 0.5)
        self.assertEqual(counts[1, "MID"], 0.5)
        self.assertEqual(sum(counts.values()), 1)

    def test_multiple_roles_in_same_group_are_deduplicated(self):
        self.assertEqual(
            player_positions([1, 2, 3], {1: "DEF", 2: "DEF", 3: "MID"}), ["DEF", "MID"]
        )

    def test_all_held_positions_are_considered(self):
        counts = {(1, "ATT"): 1, (1, "MID"): 1, (2, "DEF"): 2}
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 2, 2: 2},
                {},
                None,
                5,
                player_positions=["ATT", "MID"],
                position_counts=counts,
            ),
            2,
        )

    def test_pending_reservations_can_change_position_preference(self):
        counts = position_occupancy(
            [
                {"country": 1, "positions": ["ATT"]},
                {"country": 1, "positions": ["ATT"]},
                {"country": 2, "positions": ["GK"]},
            ]
        )
        self.assertEqual(
            choose_country(
                [1, 2], {1: 2, 2: 2}, {}, None, 5, player_positions=["ATT"], position_counts=counts
            ),
            2,
        )

    def test_club_capacity_remains_a_hard_constraint(self):
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 2, 2: 2},
                {(2, 8): 5},
                8,
                5,
                player_positions=["ATT"],
                position_counts={(1, "ATT"): 2},
            ),
            1,
        )

    def test_country_size_still_takes_priority(self):
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 1, 2: 2},
                {},
                None,
                5,
                player_positions=["ATT"],
                position_counts={(1, "ATT"): 1},
            ),
            1,
        )

    def test_no_positions_uses_normal_balancing(self):
        self.assertEqual(
            choose_country(
                [1, 2],
                {1: 1, 2: 2},
                {},
                None,
                5,
                player_positions=[],
                position_counts={(1, "ATT"): 1},
            ),
            1,
        )

    def test_no_minimums_or_required_positions(self):
        self.assertEqual(
            choose_country([1], {}, {}, None, 5, player_positions=["ATT"], position_counts={}), 1
        )
        self.assertEqual(player_positions([99], {1: "GK"}), [])
