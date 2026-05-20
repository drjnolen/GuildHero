import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "GuildHero"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from sui_utils import (
    build_airdrop_balance_requirements,
    decrypt_private_key,
    derive_sui_address,
    encrypt_private_key,
    normalize_sui_private_key,
    resolve_airdrop_sender_config,
)


class SuiUtilsTests(unittest.TestCase):
    def test_normalize_sui_private_key_accepts_valid_hex(self):
        raw = "A" * 64
        self.assertEqual(normalize_sui_private_key(raw), "a" * 64)
        self.assertEqual(normalize_sui_private_key("0x" + raw), "a" * 64)

    def test_normalize_sui_private_key_rejects_invalid_values(self):
        self.assertIsNone(normalize_sui_private_key(""))
        self.assertIsNone(normalize_sui_private_key("0x1234"))
        self.assertIsNone(normalize_sui_private_key("z" * 64))

    def test_derive_sui_address_is_stable(self):
        private_key = "1" * 64
        self.assertEqual(
            derive_sui_address(private_key),
            "0x0881c07520943bbf13989b92892093c1b50672156fa5f873c22892701cb2e207",
        )

    def test_encrypt_and_decrypt_private_key_round_trip(self):
        private_key = "2" * 64
        encryption_key = "3" * 64
        encrypted = encrypt_private_key(private_key, encryption_key)
        self.assertNotIn(private_key, encrypted)
        self.assertEqual(decrypt_private_key(encrypted, encryption_key), private_key)

    def test_encrypt_private_key_can_use_environment_key(self):
        private_key = "4" * 64
        os.environ["AIRDROP_ENCRYPTION_KEY"] = "5" * 64
        try:
            encrypted = encrypt_private_key(private_key)
            self.assertEqual(decrypt_private_key(encrypted), private_key)
        finally:
            os.environ.pop("AIRDROP_ENCRYPTION_KEY", None)

    def test_build_airdrop_balance_requirements_for_native_sui(self):
        requirements = build_airdrop_balance_requirements(3, 100, "0x2::sui::SUI", 5)
        self.assertEqual(requirements["required_sui_balance"], 315)
        self.assertEqual(requirements["required_token_balance"], 0)

    def test_build_airdrop_balance_requirements_for_custom_token(self):
        requirements = build_airdrop_balance_requirements(3, 100, "0xcustom::coin::TOKEN", 5)
        self.assertEqual(requirements["required_sui_balance"], 15)
        self.assertEqual(requirements["required_token_balance"], 300)

    def test_resolve_airdrop_sender_config_prefers_group_wallet(self):
        os.environ["AIRDROP_ENCRYPTION_KEY"] = "7" * 64
        try:
            group_private_key = "8" * 64
            resolved = resolve_airdrop_sender_config(
                {
                    "wallet_address": derive_sui_address(group_private_key),
                    "encrypted_private_key": encrypt_private_key(group_private_key),
                },
                env_private_key="9" * 64,
            )
            self.assertEqual(resolved["source"], "group")
            self.assertEqual(resolved["private_key_hex"], group_private_key)
        finally:
            os.environ.pop("AIRDROP_ENCRYPTION_KEY", None)

    def test_resolve_airdrop_sender_config_falls_back_to_environment(self):
        env_private_key = "a" * 64
        resolved = resolve_airdrop_sender_config(None, env_private_key=env_private_key)
        self.assertEqual(resolved["source"], "environment")
        self.assertEqual(resolved["private_key_hex"], env_private_key)
        self.assertEqual(resolved["wallet_address"], derive_sui_address(env_private_key))


if __name__ == "__main__":
    unittest.main()
