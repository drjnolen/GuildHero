import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "GuildHero"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from raffle_utils import get_raffle_rank_weight, select_weighted_raffle_winner


class FakeRandom:
    def __init__(self):
        self.population = None
        self.weights = None
        self.k = None

    def choices(self, population, weights, k):
        self.population = population
        self.weights = weights
        self.k = k
        return [population[1] if len(population) > 1 else population[0]]


class RaffleUtilsTests(unittest.TestCase):
    def test_raffle_weights_decrease_with_rank(self):
        self.assertGreater(get_raffle_rank_weight(1), get_raffle_rank_weight(10))
        self.assertGreater(get_raffle_rank_weight(10), get_raffle_rank_weight(20))
        self.assertGreater(get_raffle_rank_weight(20), 0)

    def test_select_weighted_raffle_winner_uses_rank_based_weights(self):
        candidates = [
            {"rank": 1, "username": "alpha"},
            {"rank": 7, "username": "beta"},
            {"rank": 20, "username": "gamma"},
        ]
        fake_random = FakeRandom()

        winner = select_weighted_raffle_winner(candidates, rng=fake_random)

        self.assertEqual(winner, candidates[1])
        self.assertEqual(fake_random.population, candidates)
        self.assertEqual(
            fake_random.weights,
            [
                get_raffle_rank_weight(1),
                get_raffle_rank_weight(7),
                get_raffle_rank_weight(20),
            ],
        )
        self.assertEqual(fake_random.k, 1)


if __name__ == "__main__":
    unittest.main()
