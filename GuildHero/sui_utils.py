import base64
import hashlib
import os
import re

from nacl.secret import SecretBox
from nacl.signing import SigningKey

_HEX_32_BYTE_REGEX = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
DEFAULT_SUI_COIN_TYPE = "0x2::sui::SUI"
ENCRYPTION_KEY_ENV = "AIRDROP_ENCRYPTION_KEY"


def normalize_sui_private_key(private_key: str | None) -> str | None:
    candidate = (private_key or "").strip()
    if not _HEX_32_BYTE_REGEX.fullmatch(candidate):
        return None
    return (candidate[2:] if candidate.startswith("0x") else candidate).lower()


def get_sui_signing_key(private_key: str) -> SigningKey:
    normalized = normalize_sui_private_key(private_key)
    if not normalized:
        raise ValueError("Invalid SUI private key format. Expected 64 hex characters (32-byte Ed25519 key).")
    return SigningKey(bytes.fromhex(normalized))


def derive_sui_address(private_key: str) -> str:
    signing_key = get_sui_signing_key(private_key)
    public_key = signing_key.verify_key.encode()
    addr_hash = hashlib.blake2b(b"\x00" + public_key, digest_size=32).digest()
    return "0x" + addr_hash.hex()


def _load_encryption_key(encryption_key: str | None = None) -> bytes:
    source = encryption_key or os.environ.get(ENCRYPTION_KEY_ENV)
    normalized = normalize_sui_private_key(source or "")
    if not normalized:
        raise ValueError(
            f"{ENCRYPTION_KEY_ENV} must be set to a 32-byte hex key (64 hex characters) to store per-group airdrop wallets."
        )
    return bytes.fromhex(normalized)


def encrypt_private_key(private_key: str, encryption_key: str | None = None) -> str:
    normalized_private_key = normalize_sui_private_key(private_key)
    if not normalized_private_key:
        raise ValueError("Invalid SUI private key format. Expected 64 hex characters (32-byte Ed25519 key).")
    box = SecretBox(_load_encryption_key(encryption_key))
    encrypted = box.encrypt(bytes.fromhex(normalized_private_key))
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_private_key(encrypted_private_key: str, encryption_key: str | None = None) -> str:
    box = SecretBox(_load_encryption_key(encryption_key))
    decrypted = box.decrypt(base64.b64decode(encrypted_private_key))
    return decrypted.hex()


def build_airdrop_balance_requirements(recipient_count: int, amount: int, coin_type: str, gas_budget: int) -> dict:
    required_token_balance = amount * recipient_count
    required_sui_balance = gas_budget * recipient_count

    if coin_type == DEFAULT_SUI_COIN_TYPE:
        required_sui_balance += required_token_balance
        required_token_balance = 0

    return {
        "required_sui_balance": required_sui_balance,
        "required_token_balance": required_token_balance,
    }


def resolve_airdrop_sender_config(group_wallet: dict | None, env_private_key: str | None = None) -> dict | None:
    if group_wallet and group_wallet.get("encrypted_private_key"):
        private_key_hex = decrypt_private_key(group_wallet["encrypted_private_key"])
        return {
            "private_key_hex": private_key_hex,
            "wallet_address": group_wallet.get("wallet_address") or derive_sui_address(private_key_hex),
            "source": "group",
        }

    if not env_private_key:
        return None

    normalized_private_key = normalize_sui_private_key(env_private_key)
    if not normalized_private_key:
        raise ValueError("Invalid SUI_PRIVATE_KEY format. Expected 64 hex characters (32-byte Ed25519 key).")

    return {
        "private_key_hex": normalized_private_key,
        "wallet_address": derive_sui_address(normalized_private_key),
        "source": "environment",
    }
