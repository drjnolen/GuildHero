import base64
import hashlib
import os
import re

from nacl.secret import SecretBox
from nacl.signing import SigningKey

_HEX_32_BYTE_REGEX = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CHARSET_MAP = {char: index for index, char in enumerate(_BECH32_CHARSET)}
_BECH32_CHECKSUM_LENGTH = 6
_BECH32_CONST = 1
_SUI_PRIVATE_KEY_HRP = "suiprivkey"
_ED25519_SUI_KEY_SCHEME = 0x00
DEFAULT_SUI_COIN_TYPE = "0x2::sui::SUI"
ENCRYPTION_KEY_ENV = "AIRDROP_ENCRYPTION_KEY"


def _normalize_hex_32_byte(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not _HEX_32_BYTE_REGEX.fullmatch(candidate):
        return None
    return (candidate[2:] if candidate.startswith("0x") else candidate).lower()


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                checksum ^= generator[i]
    return checksum


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == _BECH32_CONST


def _bech32_decode(value: str) -> tuple[str, list[int]] | tuple[None, None]:
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        return None, None
    if value.lower() != value and value.upper() != value:
        return None, None

    normalized = value.lower()
    separator_index = normalized.rfind("1")
    if separator_index <= 0 or separator_index + _BECH32_CHECKSUM_LENGTH >= len(normalized):
        return None, None

    hrp = normalized[:separator_index]
    data = []
    for char in normalized[separator_index + 1 :]:
        decoded = _BECH32_CHARSET_MAP.get(char)
        if decoded is None:
            return None, None
        data.append(decoded)

    if not _bech32_verify_checksum(hrp, data):
        return None, None
    return hrp, data[:-_BECH32_CHECKSUM_LENGTH]


def _convertbits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> bytes | None:
    accumulator = 0
    bits = 0
    result = bytearray()
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1

    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)

    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None

    return bytes(result)


def _normalize_sui_bech32_private_key(private_key: str | None) -> str | None:
    candidate = (private_key or "").strip()
    hrp, encoded_key = _bech32_decode(candidate)
    if hrp != _SUI_PRIVATE_KEY_HRP or encoded_key is None:
        return None

    decoded_key = _convertbits(encoded_key, 5, 8, False)
    if not decoded_key or len(decoded_key) != 33 or decoded_key[0] != _ED25519_SUI_KEY_SCHEME:
        return None

    return decoded_key[1:].hex()


def normalize_sui_private_key(private_key: str | None) -> str | None:
    return _normalize_hex_32_byte(private_key) or _normalize_sui_bech32_private_key(private_key)


def get_sui_signing_key(private_key: str) -> SigningKey:
    normalized = normalize_sui_private_key(private_key)
    if not normalized:
        raise ValueError("Invalid SUI private key format. Expected suiprivkey1... or 64 hex characters (32-byte Ed25519 key).")
    return SigningKey(bytes.fromhex(normalized))


def derive_sui_address(private_key: str) -> str:
    signing_key = get_sui_signing_key(private_key)
    public_key = signing_key.verify_key.encode()
    addr_hash = hashlib.blake2b(b"\x00" + public_key, digest_size=32).digest()
    return "0x" + addr_hash.hex()


def _load_encryption_key(encryption_key: str | None = None) -> bytes:
    source = encryption_key or os.environ.get(ENCRYPTION_KEY_ENV)
    normalized = _normalize_hex_32_byte(source or "")
    if not normalized:
        raise ValueError(
            f"{ENCRYPTION_KEY_ENV} must be set to a 32-byte hex key (64 hex characters) to store per-group airdrop wallets."
        )
    return bytes.fromhex(normalized)


def encrypt_private_key(private_key: str, encryption_key: str | None = None) -> str:
    normalized_private_key = normalize_sui_private_key(private_key)
    if not normalized_private_key:
        raise ValueError("Invalid SUI private key format. Expected suiprivkey1... or 64 hex characters (32-byte Ed25519 key).")
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
        raise ValueError("Invalid SUI_PRIVATE_KEY format. Expected suiprivkey1... or 64 hex characters (32-byte Ed25519 key).")

    return {
        "private_key_hex": normalized_private_key,
        "wallet_address": derive_sui_address(normalized_private_key),
        "source": "environment",
    }
