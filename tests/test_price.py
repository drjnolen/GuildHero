"""Unit tests for the fetch_crypto_price function in bot.py.

We mock the database, Telegram, and http_clients modules so that the tests
exercise only the price lookup logic without real external calls.
"""

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "CityLedger"

# Provide a dummy DATABASE_URL so the db module doesn't raise on import.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")

# Inject mocks for external modules before importing bot.py
for _mod in (
    "psycopg2",
    "psycopg2.extras",
    "openai",
    "nacl",
    "nacl.signing",
    "nacl.secret",
    "pytz",
    "telegram",
    "telegram.constants",
    "telegram.ext",
    "telegram.request",
):
    sys.modules.setdefault(_mod, MagicMock())

# Mock internal dependencies
sys.modules["db"] = MagicMock()
sys.modules["ai_services"] = MagicMock()
sys.modules["raffle_utils"] = MagicMock()
sys.modules["sui_utils"] = MagicMock()
sys.modules["http_clients"] = MagicMock()

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import bot  # noqa: E402

# Clean up mocks for modules that have their own test files
for _mod_name in ("sui_utils", "raffle_utils", "nacl", "nacl.signing", "nacl.secret"):
    sys.modules.pop(_mod_name, None)


class TestPriceFetching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Clear the in-memory price cache before each test
        bot._price_cache.clear()
        bot._token_volume_cache.clear()

    async def test_preferred_alias_lookup(self):
        # Test that preferred aliases like SUI bypass the Search API
        # and directly call coins/markets.
        mock_client = AsyncMock()

        # Patch get_shared_async_client in the bot module directly
        with patch("bot.get_shared_async_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            # Mock the response for coins/markets
            mock_markets_resp = MagicMock()
            mock_markets_resp.json.return_value = [
                {
                    "id": "sui",
                    "symbol": "sui",
                    "name": "Sui",
                    "current_price": 1.50,
                    "price_change_percentage_24h": 5.0,
                    "market_cap": 1500000000,
                    "total_volume": 300000000,
                    "market_cap_rank": 50,
                }
            ]
            mock_markets_resp.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_markets_resp

            # Use "SUI" since it is in the SUI_PRICE_ALIASES dictionary
            result = await bot.fetch_crypto_price("SUI")

            # Verify result is correct
            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "Sui")
            self.assertEqual(result["symbol"], "SUI")
            self.assertEqual(result["price"], 1.50)
            self.assertEqual(result["change_24h"], 5.0)

            # Verify get call parameters to markets (and no search call)
            mock_client.get.assert_called_once()
            args, kwargs = mock_client.get.call_args
            self.assertIn("coins/markets", args[0])
            self.assertEqual(kwargs["params"]["ids"], "sui")

    async def test_general_alias_lookup(self):
        # Test that general aliases like BTC also bypass the Search API
        # and directly call coins/markets.
        mock_client = AsyncMock()

        with patch("bot.get_shared_async_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            # Mock the response for coins/markets
            mock_markets_resp = MagicMock()
            mock_markets_resp.json.return_value = [
                {
                    "id": "bitcoin",
                    "symbol": "btc",
                    "name": "Bitcoin",
                    "current_price": 60000.0,
                    "price_change_percentage_24h": -2.0,
                    "market_cap": 1100000000000,
                    "total_volume": 25000000000,
                    "market_cap_rank": 1,
                }
            ]
            mock_markets_resp.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_markets_resp

            result = await bot.fetch_crypto_price("BTC")

            # Verify result is correct
            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "Bitcoin")
            self.assertEqual(result["symbol"], "BTC")
            self.assertEqual(result["price"], 60000.0)
            self.assertEqual(result["change_24h"], -2.0)

            # Verify get call parameters to markets (and no search call)
            mock_client.get.assert_called_once()
            args, kwargs = mock_client.get.call_args
            self.assertIn("coins/markets", args[0])
            self.assertEqual(kwargs["params"]["ids"], "bitcoin")

    async def test_search_fallback_lookup(self):
        # Test that unknown symbols trigger search and then markets lookup.
        mock_client = AsyncMock()

        # Patch get_shared_async_client in the bot module directly
        with patch("bot.get_shared_async_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            # Mock search response
            mock_search_resp = MagicMock()
            mock_search_resp.json.return_value = {
                "coins": [
                    {"id": "unknown-coin", "symbol": "unk", "name": "Unknown Coin"}
                ]
            }
            mock_search_resp.raise_for_status = MagicMock()

            # Mock markets response
            mock_markets_resp = MagicMock()
            mock_markets_resp.json.return_value = [
                {
                    "id": "unknown-coin",
                    "symbol": "unk",
                    "name": "Unknown Coin",
                    "current_price": 1.23,
                    "price_change_percentage_24h": -1.5,
                    "market_cap": 123000,
                    "total_volume": 456,
                    "market_cap_rank": 500,
                }
            ]
            mock_markets_resp.raise_for_status = MagicMock()

            # Set up mock responses for both GET requests
            mock_client.get.side_effect = [mock_search_resp, mock_markets_resp]

            result = await bot.fetch_crypto_price("UNK")

            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "Unknown Coin")
            self.assertEqual(result["price"], 1.23)
            self.assertEqual(mock_client.get.call_count, 2)

            # Check first call (search)
            first_args, first_kwargs = mock_client.get.call_args_list[0]
            self.assertIn("/search", first_args[0])
            self.assertEqual(first_kwargs["params"]["query"], "UNK")

            # Check second call (markets)
            second_args, second_kwargs = mock_client.get.call_args_list[1]
            self.assertIn("coins/markets", second_args[0])
            self.assertEqual(second_kwargs["params"]["ids"], "unknown-coin")

    async def test_known_sui_tokens_fall_back_to_address_specific_dex_pairs(self):
        for symbol in ("CITY", "MANIFEST"):
            with self.subTest(symbol=symbol):
                bot._price_cache.clear()
                coin_type = bot.SUI_DEXSCREENER_TOKEN_ADDRESSES[symbol.lower()]
                markets_response = MagicMock()
                markets_response.json.return_value = []
                markets_response.raise_for_status = MagicMock()

                dex_response = MagicMock()
                dex_response.json.return_value = [
                    {
                        "chainId": "sui",
                        "baseToken": {
                            "address": "0xspoof::coin::COIN",
                            "name": symbol,
                            "symbol": symbol,
                        },
                        "priceUsd": "999",
                        "liquidity": {"usd": 99_000_000},
                    },
                    {
                        "chainId": "sui",
                        "baseToken": {
                            "address": coin_type,
                            "name": "Alpha City" if symbol == "CITY" else "MANIFEST",
                            "symbol": symbol,
                        },
                        "priceUsd": "0.00042",
                        "priceChange": {"h24": "-3.13"},
                        "liquidity": {"usd": 23_000},
                        "volume": {"h24": 674},
                        "marketCap": 309_000,
                    }
                ]
                dex_response.raise_for_status = MagicMock()
                mock_client = AsyncMock()
                mock_client.get.side_effect = [markets_response, dex_response]

                with patch(
                    "bot.get_shared_async_client",
                    new=AsyncMock(return_value=mock_client),
                ):
                    result = await bot.fetch_crypto_price(symbol)

                self.assertIsNotNone(result)
                self.assertEqual(result["symbol"], symbol)
                self.assertEqual(result["price"], Decimal("0.00042"))
                self.assertEqual(result["change_24h"], Decimal("-3.13"))
                dex_url = mock_client.get.await_args_list[-1].args[0]
                self.assertIn("/token-pairs/v1/sui/", dex_url)
                self.assertIn("%3A%3A", dex_url)

    async def test_general_asset_uses_cross_chain_dex_fallback_on_provider_error(self):
        dex_response = MagicMock()
        dex_response.json.return_value = {
            "pairs": [
                {
                    "chainId": "ethereum",
                    "baseToken": {
                        "address": "0xbtc",
                        "name": "Bitcoin",
                        "symbol": "BTC",
                    },
                    "priceUsd": "65000",
                    "priceChange": {"h24": 1.25},
                    "liquidity": {"usd": 5_000_000},
                    "volume": {"h24": 2_000_000},
                    "marketCap": 1_200_000_000_000,
                }
            ]
        }
        dex_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get.side_effect = [RuntimeError("CoinGecko unavailable"), dex_response]

        with patch(
            "bot.get_shared_async_client",
            new=AsyncMock(return_value=mock_client),
        ):
            result = await bot.fetch_crypto_price("BTC")

        self.assertEqual(result["price"], Decimal("65000"))
        self.assertIn("/latest/dex/search", mock_client.get.await_args_list[-1].args[0])

    async def test_unknown_asset_dex_fallback_is_restricted_to_exact_sui_match(self):
        search_response = MagicMock()
        search_response.json.return_value = {"coins": []}
        search_response.raise_for_status = MagicMock()
        dex_response = MagicMock()
        dex_response.json.return_value = {
            "pairs": [
                {
                    "chainId": "solana",
                    "baseToken": {"address": "sol", "name": "Mystery", "symbol": "MYSTERY"},
                    "priceUsd": "99",
                    "liquidity": {"usd": 10_000_000},
                },
                {
                    "chainId": "sui",
                    "baseToken": {"address": "0xsui", "name": "Mystery", "symbol": "MYSTERY"},
                    "priceUsd": "0.25",
                    "liquidity": {"usd": 10_000},
                    "volume": {"h24": 500},
                },
                {
                    "chainId": "sui",
                    "baseToken": {"address": "0xother", "name": "Other", "symbol": "OTHER"},
                    "priceUsd": "1000",
                    "liquidity": {"usd": 20_000_000},
                },
            ]
        }
        dex_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get.side_effect = [search_response, dex_response]

        with patch(
            "bot.get_shared_async_client",
            new=AsyncMock(return_value=mock_client),
        ):
            result = await bot.fetch_crypto_price("MYSTERY")

        self.assertEqual(result["price"], Decimal("0.25"))
        self.assertEqual(result["symbol"], "MYSTERY")

    async def test_exact_sui_buy_valuation_only_fetches_sui_price(self):
        event = SimpleNamespace(
            amount=10_000_000_000,
            sui_spent=2_000_000_000,
        )
        with patch(
            "bot.fetch_crypto_price",
            new=AsyncMock(return_value={"price": 1.5}),
        ) as mock_fetch:
            valuation = await bot._get_buy_valuation(
                event,
                {"symbol": "CITY", "decimals": 9},
            )

        self.assertEqual(valuation["sui"], Decimal("2"))
        self.assertEqual(valuation["usd"], Decimal("3.0"))
        mock_fetch.assert_awaited_once_with("SUI")

    async def test_cross_token_buy_valuation_fetches_token_and_sui_prices(self):
        event = SimpleNamespace(
            amount=10_000_000_000,
            sui_spent=None,
        )

        async def price_for(symbol):
            return {"price": 4 if symbol == "SUI" else 2}

        with patch(
            "bot.fetch_crypto_price",
            new=AsyncMock(side_effect=price_for),
        ) as mock_fetch:
            valuation = await bot._get_buy_valuation(
                event,
                {"symbol": "CITY", "decimals": 9},
            )

        self.assertEqual(valuation["sui"], Decimal("5"))
        self.assertEqual(valuation["usd"], Decimal("20"))
        self.assertCountEqual(
            [call.args[0] for call in mock_fetch.await_args_list],
            ["SUI", "CITY"],
        )

    async def test_price_response_keeps_city_link_without_web_preview(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(args=["SUI"])
        price_data = {
            "name": "Sui",
            "symbol": "SUI",
            "price": 1.5,
            "change_24h": 2.0,
            "market_cap": 1_000_000,
            "volume_24h": 100_000,
        }

        with patch(
            "bot.fetch_crypto_price",
            new=AsyncMock(return_value=price_data),
        ):
            await bot.price_command(update, context)

        final_reply = message.reply_text.await_args_list[-1]
        self.assertIn(bot.FOOTER_HTML, final_reply.args[0])
        self.assertIn("https://app.noodles.fi/coins/", final_reply.args[0])
        self.assertTrue(final_reply.kwargs["disable_web_page_preview"])

    async def test_aggregates_unique_sui_pair_volume_and_caches_it(self):
        coin_type = "0xabc::city::CITY"
        pair = {
            "chainId": "sui",
            "pairAddress": "0xpair1",
            "baseToken": {"address": coin_type},
            "quoteToken": {"address": "0x2::sui::SUI"},
            "volume": {"h1": 125.5, "h24": 1000},
        }
        second_pair = {
            "chainId": "sui",
            "pairAddress": "0xpair2",
            "baseToken": {"address": "0x2::sui::SUI"},
            "quoteToken": {"address": coin_type},
            "volume": {"h1": "24.5", "h24": "250"},
        }
        unrelated_pair = {
            "chainId": "sui",
            "pairAddress": "0xother",
            "baseToken": {"address": "0xdef::other::OTHER"},
            "quoteToken": {"address": "0x2::sui::SUI"},
            "volume": {"h1": 999, "h24": 999},
        }
        response = MagicMock()
        response.json.return_value = [
            pair,
            pair,  # A duplicate pair must not be double-counted.
            second_pair,
            unrelated_pair,
        ]
        response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get.return_value = response

        with patch(
            "bot.get_shared_async_client",
            new=AsyncMock(return_value=mock_client),
        ):
            first = await bot.fetch_token_volume(coin_type)
            second = await bot.fetch_token_volume(coin_type)

        self.assertEqual(
            first,
            {"h1": Decimal("150.0"), "h24": Decimal("1250")},
        )
        self.assertEqual(second, first)
        mock_client.get.assert_awaited_once()
        request_url = mock_client.get.await_args.args[0]
        self.assertIn("/token-pairs/v1/sui/", request_url)
        self.assertIn("%3A%3Acity%3A%3ACITY", request_url)

    async def test_volume_api_failure_is_negative_cached(self):
        mock_client = AsyncMock()
        mock_client.get.side_effect = RuntimeError("provider unavailable")

        with patch(
            "bot.get_shared_async_client",
            new=AsyncMock(return_value=mock_client),
        ):
            first = await bot.fetch_token_volume("0xabc::city::CITY")
            second = await bot.fetch_token_volume("0xabc::city::CITY")

        self.assertIsNone(first)
        self.assertIsNone(second)
        mock_client.get.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
