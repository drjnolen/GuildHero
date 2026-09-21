"""Command parsing and bounded payout planning; no network or wallet side effects."""
import re

BATCH_SIZE = 25
MAX_RANK = 1000
U64_MAX = (1 << 64) - 1


def parse_reward(text: str, decimals: int) -> int:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?([kKmM]?)", text)
    if not match or len(text) > 80:
        raise ValueError("Use a positive amount, optionally ending in k or m (e.g. 30000 or 30k).")
    whole, fraction, suffix = match.groups()
    fraction = fraction or ""
    scale = decimals + {"": 0, "k": 3, "m": 6}[suffix.lower()]
    if len(fraction) > scale:
        raise ValueError(f"Amount supports up to {decimals} token decimal places.")
    amount = int(whole + fraction) * 10 ** (scale - len(fraction))
    if not 0 < amount <= U64_MAX:
        raise ValueError("Amount must be positive and fit Sui's u64 coin balance.")
    return amount


def parse_airdrop_tiers(args: list[str], decimals: int) -> list[tuple[int, int, int]]:
    """Return inclusive contiguous rank ranges, with amounts in base units."""
    if len(args) == 2 and all(":" not in item for item in args):
        if not re.fullmatch(r"[0-9]+", args[0]):
            raise ValueError("Count must be a positive whole number.")
        args = [f"1-{args[0]}:{args[1]}"]
    if not args:
        raise ValueError("Use /airdrop 10 10000 or /airdrop 1-5:30k 6-10:15k.")
    tiers = []
    next_rank = 1
    for arg in args:
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?:(.+)", arg)
        if not match:
            raise ValueError("Use count amount, or rank:amount / first-last:amount tiers.")
        start, end = int(match[1]), int(match[2] or match[1])
        if start != next_rank or end < start or end > MAX_RANK:
            raise ValueError(f"Tiers must cover consecutive ranks starting at 1, without overlaps, up to {MAX_RANK}.")
        tiers.append((start, end, parse_reward(match[3], decimals)))
        next_rank = end + 1
    return tiers


def plan_batches(recipients: list[dict]) -> list[list[dict]]:
    batches = [recipients[i:i + BATCH_SIZE] for i in range(0, len(recipients), BATCH_SIZE)]
    for batch in batches:
        if sum(int(item["amount"]) for item in batch) > U64_MAX:
            raise ValueError("Batch total exceeds Sui's u64 coin balance.")
    return batches
