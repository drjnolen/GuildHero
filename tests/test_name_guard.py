import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parents[1] / "CityLedger"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from name_guard import evaluate_name_guard, normalized_name_words  # noqa: E402


def _user(user_id, full_name, *, username=None, is_bot=False):
    parts = full_name.split(" ", 1)
    return SimpleNamespace(
        id=user_id,
        full_name=full_name,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else None,
        username=username,
        is_bot=is_bot,
    )


class TestProtectedWords(unittest.TestCase):
    def test_matches_complete_words_in_names_and_usernames(self):
        cases = (
            _user(1, "CITY Dev"),
            _user(2, "ADMIN"),
            _user(3, "Alice", username="official_support"),
            _user(4, "CITY a\u200bdmin"),
            _user(5, "CITY аdmin"),
        )

        self.assertEqual(
            [evaluate_name_guard(user).kind for user in cases],
            ["protected_word"] * len(cases),
        )

    def test_does_not_match_substrings(self):
        for full_name in ("Devin", "Administrator", "Supporter", "DevOps"):
            with self.subTest(full_name=full_name):
                self.assertIsNone(evaluate_name_guard(_user(1, full_name)))

    def test_punctuation_and_underscore_are_word_boundaries(self):
        self.assertEqual(
            normalized_name_words("official_admin-support"),
            ("official", "admin", "support"),
        )


class TestProtectedAdminIdentities(unittest.TestCase):
    def setUp(self):
        self.admin = _user(
            7,
            "Julia Nolen",
            username="JuliaAdminAccount",
        )
        self.administrators = [SimpleNamespace(user=self.admin)]

    def test_matches_normalized_admin_display_name(self):
        match = evaluate_name_guard(
            _user(99, "Júlia-Nolen"),
            self.administrators,
        )
        self.assertEqual(match.kind, "admin_identity")

    def test_matches_admin_username_used_as_display_name(self):
        match = evaluate_name_guard(
            _user(99, "JuliaAdminAccount"),
            self.administrators,
        )
        self.assertEqual(match.kind, "admin_identity")

    def test_real_admin_and_bots_are_exempt(self):
        self.assertIsNone(evaluate_name_guard(self.admin, self.administrators))
        protected_word_admin = _user(8, "CITY Dev")
        self.assertIsNone(
            evaluate_name_guard(
                protected_word_admin,
                [SimpleNamespace(user=protected_word_admin)],
            )
        )
        self.assertIsNone(
            evaluate_name_guard(
                _user(99, "CITY Admin", is_bot=True),
                self.administrators,
            )
        )

    def test_unrelated_member_is_allowed(self):
        self.assertIsNone(
            evaluate_name_guard(
                _user(99, "Community Member", username="cityfan"),
                self.administrators,
            )
        )


if __name__ == "__main__":
    unittest.main()
