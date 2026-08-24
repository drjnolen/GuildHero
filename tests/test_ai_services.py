"""Regression tests for the cost-bounded OpenAI service layer."""

import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "CityLedger"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

_saved_modules = {
    name: sys.modules.get(name)
    for name in ("openai", "telegram_utils")
}

fake_openai = ModuleType("openai")
fake_openai.OpenAI = MagicMock()
sys.modules["openai"] = fake_openai

fake_telegram_utils = ModuleType("telegram_utils")
fake_telegram_utils.sanitize_html_for_telegram = lambda text: text
sys.modules["telegram_utils"] = fake_telegram_utils

import ai_services  # noqa: E402

for _name, _module in _saved_modules.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


def _completion(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class TestAIServiceCaching(unittest.TestCase):
    def setUp(self):
        ai_services.clear_ai_result_cache()
        self.create = MagicMock(
            return_value=_completion(
                '{"quality": 12, "tone": 13, "helpfulness": 14, "humor": 15}'
            )
        )
        ai_services._openai_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )
        )

    def tearDown(self):
        ai_services.clear_ai_result_cache()
        ai_services._openai_client = None

    def test_exact_analysis_request_is_cached(self):
        first = ai_services.analyze_user_messages("alice", 2, "hello\nworld")
        second = ai_services.analyze_user_messages("alice", 2, "hello\nworld")

        self.assertEqual(first, second)
        self.assertEqual(self.create.call_count, 1)
        request = self.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5-nano")
        self.assertEqual(request["reasoning_effort"], "minimal")
        self.assertIn("max_completion_tokens", request)
        self.assertNotIn("max_tokens", request)
        self.assertNotIn("temperature", request)

    def test_transcript_change_is_a_cache_miss(self):
        ai_services.analyze_user_messages("alice", 1, "first transcript")
        ai_services.analyze_user_messages("alice", 1, "changed transcript")

        self.assertEqual(self.create.call_count, 2)

    def test_invalid_json_is_not_cached(self):
        self.create.side_effect = [
            _completion("not-json"),
            _completion(
                '{"quality": 1, "tone": 2, "helpfulness": 3, "humor": 4}'
            ),
        ]

        fallback = ai_services.analyze_user_messages("alice", 1, "same")
        recovered = ai_services.analyze_user_messages("alice", 1, "same")

        self.assertEqual(
            fallback,
            {"quality": 8, "tone": 10, "helpfulness": 8, "humor": 8},
        )
        self.assertEqual(recovered["quality"], 1)
        self.assertEqual(self.create.call_count, 2)

    def test_concurrent_identical_requests_share_one_api_call(self):
        def create_completion(**_kwargs):
            time.sleep(0.05)
            return _completion("same generated text")

        self.create.side_effect = create_completion
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    ai_services.generate_copypasta,
                    ["identical transcript", "identical transcript"],
                )
            )

        self.assertEqual(results, ["same generated text", "same generated text"])
        self.assertEqual(self.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
