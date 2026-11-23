import unittest

from bot.services.rules import callup_chunks, choose_country, occupancy, primary_club


class DraftRulesTests(unittest.TestCase):
    def test_balance_prioritizes_smallest_eligible(self):
        self.assertEqual(choose_country([1, 2, 3], {1: 10, 2: 1, 3: 2}, {(2, 8): 5}, 8, 5), 3)

    def test_random_tie_has_all_and_only_smallest_candidates(self):
        observed = []

        def choose(candidates):
            observed.extend(candidates)
            return candidates[-1]

        self.assertEqual(
            choose_country([1, 2, 3], {1: 4, 2: 2, 3: 2}, {}, None, 5, chooser=choose), 3
        )
        self.assertEqual(observed, [2, 3])

    def test_reservations_and_active_roster_share_capacity(self):
        pop, counts = occupancy([{"country": 1, "club": 8}] * 5)
        self.assertEqual(pop[1], 5)
        self.assertIsNone(choose_country([1], pop, counts, 8, 5))

    def test_unaffiliated_has_no_club_capacity(self):
        self.assertEqual(choose_country([1], {1: 20}, {(1, 8): 20}, None, 5), 1)

    def test_no_eligible_country(self):
        self.assertIsNone(choose_country([], {}, {}, None, 5))
        self.assertIsNone(choose_country([1], {}, {}, None, 5, excluded=[1]))

    def test_respin_excludes_current_and_previous(self):
        self.assertEqual(choose_country([1, 2, 3], {}, {}, None, 5, excluded=[1, 2]), 3)

    def test_country_selector_does_not_track_reroll_usage(self):
        # Repeated proposal requests do not consume an assignment allowance.
        for _ in range(500):
            self.assertEqual(choose_country([1, 2], {}, {}, None, 5, excluded=[1]), 2)

    def test_primary_club_sticks_until_role_removed_or_disabled(self):
        self.assertEqual(primary_club([1, 2], [1, 2], 2), (2, False))
        self.assertEqual(primary_club([1], [1, 2], 2), (1, False))
        self.assertEqual(primary_club([1, 2], [1], 2), (1, False))
        self.assertEqual(primary_club([2], [1], 2), (None, False))

    def test_multiple_clubs_block_without_valid_primary(self):
        self.assertEqual(primary_club([1, 2], [1, 2]), (None, True))
        self.assertEqual(primary_club([], [1, 2]), (None, False))

    def test_lower_limit_blocks_new_assignments(self):
        self.assertIsNone(choose_country([1], {1: 5}, {(1, 8): 5}, 8, 3))

    def test_callups_are_plain_text_and_never_duplicate_players(self):
        groups = {10: list(range(100000000000000000, 100000000000000180)), 11: [200000000000000000]}
        chunks = callup_chunks(groups, country_names={10: "Ghana", 11: "France"})
        self.assertIn("**Ghana - <@&10>**", chunks[0])
        self.assertGreater(len(chunks), 1)
        text = "\n".join(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1900)
            self.assertTrue(chunk.startswith("**PCL INTERNATIONAL"))
        for players in groups.values():
            for uid in players:
                self.assertEqual(text.count(f"<@{uid}>"), 1)

    def test_no_callups_for_empty_batch(self):
        self.assertEqual(callup_chunks({}), [])


if __name__ == "__main__":
    unittest.main()
