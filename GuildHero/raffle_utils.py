import random
from collections.abc import Mapping, Sequence


RAFFLE_MAX_RANK = 20


def get_raffle_rank_weight(rank: int, max_rank: int = RAFFLE_MAX_RANK) -> float:
    """Return a slight linear rank bonus from 1.5x for rank 1 down to 1.0x for the last rank."""
    capped_rank = min(max(int(rank), 1), max_rank)
    return 1.0 + ((max_rank - capped_rank) / (max_rank * 2))


def select_weighted_raffle_winner(
    candidates: Sequence[Mapping[str, object]],
    *,
    rng: random.Random | None = None,
    max_rank: int = RAFFLE_MAX_RANK,
):
    """Pick one candidate using rank-based weights.

    Each candidate must provide a ``rank`` key. Lower rank numbers receive a
    slightly higher weight, while every candidate remains eligible.
    """
    if not candidates:
        return None

    chooser = rng or random
    weights = [get_raffle_rank_weight(candidate["rank"], max_rank=max_rank) for candidate in candidates]
    winners = chooser.choices(candidates, weights=weights, k=1)
    return winners[0] if winners else None
