"""Pure helpers for identifying Sui DEX buys from finalized transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_DEX_PACKAGES = {
    # Cetus CLMM mainnet package. Additional/upgraded packages can be supplied
    # through SUI_DEX_PACKAGES_JSON without requiring a bot release.
    "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb": "Cetus",
}

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


@dataclass(frozen=True)
class BuyEvent:
    """A selected token received as the output of a recognized swap operation."""

    digest: str
    coin_type: str
    amount: int
    wallet: str
    sender: str | None
    exchange: str
    checkpoint: int | None = None
    timestamp: Any = None


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


def _swap_evidence(transaction: Any) -> tuple[bool, list[tuple[str, str]]]:
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


def detect_buy(
    transaction: Any,
    selected_coin_type: str,
    dex_packages: Mapping[str, str] | None = None,
) -> BuyEvent | None:
    """Detect the principal recipient of a selected token in a DEX swap.

    A positive balance change by itself is deliberately insufficient because
    transfers, airdrops, rewards, and liquidity withdrawals also increase token
    balances. A swap-like Move call or event must be present.
    """

    if not selected_coin_type or not _transaction_succeeded(transaction):
        return None

    has_swap_evidence, package_clues = _swap_evidence(transaction)
    if not has_swap_evidence:
        return None

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
        if amount > 0 and address:
            received_by_wallet[address] = received_by_wallet.get(address, 0) + amount

    if not received_by_wallet:
        return None

    tx_data = _get(transaction, "transaction")
    sender = str(_get(tx_data, "sender", "") or "") or None
    if sender in received_by_wallet:
        wallet = sender
    else:
        wallet = max(received_by_wallet, key=received_by_wallet.get)

    return BuyEvent(
        digest=str(_get(transaction, "digest", "") or ""),
        coin_type=selected_coin_type,
        amount=received_by_wallet[wallet],
        wallet=wallet,
        sender=sender,
        exchange=_infer_exchange(package_clues, dex_packages),
        checkpoint=_get(transaction, "checkpoint"),
        timestamp=_get(transaction, "timestamp"),
    )
