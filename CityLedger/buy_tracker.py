"""Pure helpers for identifying Sui DEX buys from finalized transactions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_DEX_PACKAGES = {
    # Mainnet packages used by common direct and wrapped swap routes.
    "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb": "Cetus",
    "0x62a97a4997a54999ac817621c43433c482392698061c9d6aef867cac5d30d838": "Cetus",
    "0x91bfbc386a41afcfd9b2533058d7e915a1d3829089cc268ff4333d54d6339ca1": "Turbos",
    "0x8b14f4351bb342b81c27fce2fe6d0f56b98288dc88fbe60b28b26d804b25941a": "Turbos",
}
SUI_COIN_TYPE = "0x2::sui::sui"
# Full-route confirmations, not individual pool/hop events. Pin the defining
# package/type: an unrelated package can emit an identically named event.
_CONFIRMED_SWAP_TYPE = (
    "0xffd4058af7d6f6d66c335930cced8c91e38ec943e2392232aee27e8127e684e9"
    "::router::ConfirmSwapEventV3"
)

_DEX_NAME_HINTS = (
    ("cetus", "Cetus"),
    ("deepbook", "DeepBook"),
    ("turbos", "Turbos"),
    ("aftermath", "Aftermath"),
    ("flowx", "FlowX"),
    ("momentum", "Momentum"),
    ("bluefin", "Bluefin"),
    ("kriya", "Kriya"),
    ("hop", "Hop"),
)

_SWAP_OPERATION_HINTS = (
    "swap",
    "trade",
    "market_order",
    "place_order",
    "fill_order",
    "route",
)

_NON_BUY_OPERATION_HINTS = (
    "add_liquidity",
    "remove_liquidity",
    "withdraw",
    "redeem",
    "claim",
    "reward",
    "unstake",
)


@dataclass(frozen=True)
class BuyEvent:
    """A selected token received as the output of a recognized swap operation."""

    digest: str
    coin_type: str
    amount: int
    wallet: str
    sender: str | None
    exchange: str
    sui_spent: int | None = None
    checkpoint: int | None = None
    timestamp: Any = None
    # Swap output may subsequently be deposited/transferred in the same PTB.
    wallet_balance_change: int | None = None


def _get(value: Any, name: str, default=None):
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def canonicalize_sui_type(coin_type: str | None) -> str:
    """Normalize the address portion of a Move type for reliable comparisons."""

    candidate = (coin_type or "").strip().lower()
    if "::" not in candidate:
        return candidate
    address, remainder = candidate.split("::", 1)
    if address.startswith("0x"):
        address_hex = address[2:].lstrip("0") or "0"
        address = f"0x{address_hex}"
    return f"{address}::{remainder}"


def _canonicalize_package(package_id: str | None) -> str:
    candidate = (package_id or "").strip().lower()
    if candidate.startswith("0x"):
        candidate = candidate[2:].lstrip("0") or "0"
        return f"0x{candidate}"
    return candidate


def _transaction_succeeded(transaction: Any) -> bool:
    status = _get(_get(transaction, "effects"), "status")
    if status is None:
        return True
    success = _get(status, "success")
    if success is not None:
        return bool(success)
    if isinstance(status, str):
        return status.lower() == "success"
    return not bool(_get(status, "error"))


def _move_calls(transaction: Any) -> list[Any]:
    tx_data = _get(transaction, "transaction")
    tx_kind = _get(tx_data, "kind")
    programmable = _get(tx_kind, "programmable_transaction")
    commands = _get(programmable, "commands", []) or []
    return [
        move_call
        for command in commands
        if (move_call := _get(command, "move_call")) is not None
    ]


def _events(transaction: Any) -> list[Any]:
    return list(_get(_get(transaction, "events"), "events", []) or [])


def _swap_evidence(
    transaction: Any,
) -> tuple[bool, list[tuple[str, str]], list[str]]:
    """Return whether a swap-like call/event exists and venue-identifying clues."""

    descriptors: list[str] = []
    packages: list[tuple[str, str]] = []

    for move_call in _move_calls(transaction):
        package = str(_get(move_call, "package", "") or "")
        module = str(_get(move_call, "module", "") or "")
        function = str(_get(move_call, "function", "") or "")
        descriptors.append(f"{package}::{module}::{function}".lower())
        packages.append((package, f"{module}::{function}".lower()))

    for event in _events(transaction):
        package = str(_get(event, "package_id", "") or "")
        module = str(_get(event, "module", "") or "")
        event_type = str(_get(event, "event_type", "") or "")
        descriptors.append(f"{package}::{module}::{event_type}".lower())
        packages.append((package, f"{module}::{event_type}".lower()))

    return (
        any(hint in descriptor for descriptor in descriptors for hint in _SWAP_OPERATION_HINTS),
        packages,
        descriptors,
    )


def _infer_exchange(
    package_clues: list[tuple[str, str]],
    dex_packages: Mapping[str, str] | None = None,
) -> str:
    package_labels = {
        _canonicalize_package(package): label
        for package, label in {**DEFAULT_DEX_PACKAGES, **(dex_packages or {})}.items()
    }
    labels: list[str] = []

    for package, descriptor in package_clues:
        package_label = package_labels.get(_canonicalize_package(package))
        if package_label and package_label not in labels:
            labels.append(str(package_label))
        for known_package, label in package_labels.items():
            if f"{known_package}::" in descriptor and label not in labels:
                labels.append(str(label))
        for hint, label in _DEX_NAME_HINTS:
            if hint in descriptor and label not in labels:
                labels.append(label)

    return " / ".join(labels[:3]) if labels else "Unknown DEX"


def _gas_cost(transaction: Any) -> int:
    gas_used = _get(_get(transaction, "effects"), "gas_used")
    try:
        computation = int(_get(gas_used, "computation_cost", 0) or 0)
        storage = int(_get(gas_used, "storage_cost", 0) or 0)
        rebate = int(_get(gas_used, "storage_rebate", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, computation + storage - rebate)


def _wallet_net_spend(
    transaction: Any,
    wallet: str,
    coin_type: str,
) -> int:
    """Return a wallet's net spend for one coin, excluding SUI gas."""

    selected = canonicalize_sui_type(coin_type)
    wallet_lower = wallet.lower()
    net_change = 0
    for change in _get(transaction, "balance_changes", []) or []:
        address = str(_get(change, "address", "") or "").lower()
        if (
            address != wallet_lower
            or canonicalize_sui_type(_get(change, "coin_type")) != selected
        ):
            continue
        try:
            net_change += int(_get(change, "amount", 0) or 0)
        except (TypeError, ValueError):
            continue

    spend = max(0, -net_change)
    effects = _get(transaction, "effects")
    gas_payer = str(_get(effects, "gas_payer", "") or "").lower()
    if selected == SUI_COIN_TYPE and wallet_lower == gas_payer:
        spend = max(0, spend - _gas_cost(transaction))
    return spend


def _wallet_spent_another_coin(
    transaction: Any,
    wallet: str,
    selected_coin_type: str,
) -> bool:
    """Identify an input-coin outflow, excluding the wallet's SUI gas."""

    selected = canonicalize_sui_type(selected_coin_type)
    wallet_lower = wallet.lower()
    coin_types: set[str] = set()

    for change in _get(transaction, "balance_changes", []) or []:
        address = str(_get(change, "address", "") or "").lower()
        coin_type = canonicalize_sui_type(_get(change, "coin_type"))
        if address != wallet_lower or coin_type == selected:
            continue
        coin_types.add(coin_type)

    for coin_type in coin_types:
        if _wallet_net_spend(transaction, wallet, coin_type) > 0:
            return True
    return False


def _route_coin_type(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0x)?[0-9a-fA-F]{1,64}::[A-Za-z_][A-Za-z_0-9]*::[A-Za-z_][A-Za-z_0-9]*",
        value,
    ):
        return ""
    return canonicalize_sui_type(value if value.startswith("0x") else "0x" + value)


def _confirmed_swap_totals(
    events: list[Any], selected: str, sender: str | None,
) -> tuple[int, int | None] | None:
    """Sum complete routes; only report exact SUI spend for all-SUI inputs."""

    amount = spent = 0
    all_sui_inputs = True
    quote_ids = set()
    for event in events:
        data = _get(event, "parsed_json")
        if not isinstance(data, Mapping) or _get(event, "sender") != sender:
            return None
        source = _route_coin_type(data.get("from"))
        target = _route_coin_type(data.get("target"))
        # Missing types or buying then selling this token makes attribution unsafe.
        if not source or not target or source == selected:
            return None
        if target != selected:
            continue
        quote_id = data.get("quote_id")
        if not isinstance(quote_id, str) or not quote_id:
            return None
        if quote_id in quote_ids:
            return None
        quote_ids.add(quote_id)
        values = [data.get(key) for key in ("amount_in", "amount_out", "fee_amount")]
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value)
            for value in values
        ):
            return None
        route_spent, route_amount, fee = map(int, values)
        # Fee-bearing variants need their own verified accounting semantics.
        if not 0 < route_spent < 2**64 or not 0 < route_amount < 2**64 or fee:
            return None
        amount += route_amount
        if source == SUI_COIN_TYPE:
            spent += route_spent
        else:
            # Preserve the existing market-price estimate for cross-token buys;
            # their input units must never be interpreted as MIST.
            all_sui_inputs = False
    return (amount, spent if all_sui_inputs else None) if amount else None


def detect_buy(
    transaction: Any,
    selected_coin_type: str,
    dex_packages: Mapping[str, str] | None = None,
) -> BuyEvent | None:
    """Detect the principal recipient of a selected token in a DEX swap.

    Supported complete route confirmations preserve gross output and input cost.
    Otherwise, simple swaps use net wallet balances; ambiguous baskets and
    deposit/claim/liquidity operations are excluded.
    """

    if not selected_coin_type or not _transaction_succeeded(transaction):
        return None

    has_swap_evidence, package_clues, descriptors = _swap_evidence(transaction)

    selected = canonicalize_sui_type(selected_coin_type)
    received_by_wallet: dict[str, int] = {}
    for change in _get(transaction, "balance_changes", []) or []:
        if canonicalize_sui_type(_get(change, "coin_type")) != selected:
            continue
        try:
            amount = int(_get(change, "amount", 0) or 0)
        except (TypeError, ValueError):
            continue
        address = str(_get(change, "address", "") or "")
        if address:
            received_by_wallet[address] = received_by_wallet.get(address, 0) + amount

    tx_data = _get(transaction, "transaction")
    sender = str(_get(tx_data, "sender", "") or "") or None
    confirmations = [
        event for event in _events(transaction)
        if _get(event, "event_type") == _CONFIRMED_SWAP_TYPE
    ]
    if confirmations:
        totals = _confirmed_swap_totals(confirmations, selected, sender)
        if not totals or not sender:
            return None
        # Event.sender identifies the transaction sender, not necessarily the
        # recipient. Do not attribute another wallet's receipt to the sender.
        if any(
            address != sender and amount > 0
            for address, amount in received_by_wallet.items()
        ):
            return None
        amount, spent = totals
        return BuyEvent(
            digest=str(_get(transaction, "digest", "") or ""),
            coin_type=selected_coin_type,
            amount=amount,
            wallet=sender,
            sender=sender,
            exchange=_infer_exchange(package_clues, dex_packages),
            sui_spent=spent,
            checkpoint=_get(transaction, "checkpoint"),
            timestamp=_get(transaction, "timestamp"),
            wallet_balance_change=received_by_wallet.get(sender, 0),
        )

    received_by_wallet = {
        wallet: amount for wallet, amount in received_by_wallet.items() if amount > 0
    }
    if not received_by_wallet:
        return None

    if sender in received_by_wallet:
        wallet = sender
    else:
        wallet = max(received_by_wallet, key=received_by_wallet.get)

    # Without a supported route confirmation, a basket or deposit cannot safely
    # pair the whole wallet's spend with a single token's leftover balance.
    wallet_coins: dict[str, int] = {}
    for change in _get(transaction, "balance_changes", []) or []:
        if str(_get(change, "address", "")).lower() != wallet.lower():
            continue
        coin = canonicalize_sui_type(_get(change, "coin_type"))
        try:
            wallet_coins[coin] = wallet_coins.get(coin, 0) + int(_get(change, "amount", 0))
        except (TypeError, ValueError):
            continue
    has_deposit = any(
        hint in descriptor for descriptor in descriptors
        for hint in (*_NON_BUY_OPERATION_HINTS, "deposit", "::mint", "::stake")
    )
    inputs = sum(_wallet_net_spend(transaction, wallet, coin) > 0 for coin in wallet_coins)
    if has_deposit or sum(amount > 0 for amount in wallet_coins.values()) > 1 or inputs > 1:
        return None

    if not has_swap_evidence and not _wallet_spent_another_coin(
        transaction, wallet, selected_coin_type,
    ):
        return None

    return BuyEvent(
        digest=str(_get(transaction, "digest", "") or ""),
        coin_type=selected_coin_type,
        amount=received_by_wallet[wallet],
        wallet=wallet,
        sender=sender,
        exchange=_infer_exchange(package_clues, dex_packages),
        sui_spent=_wallet_net_spend(transaction, wallet, SUI_COIN_TYPE) or None,
        checkpoint=_get(transaction, "checkpoint"),
        timestamp=_get(transaction, "timestamp"),
        wallet_balance_change=received_by_wallet[wallet],
    )
