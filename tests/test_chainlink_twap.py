"""
Unit tests for ChainlinkTWAPClient and MarketResolver integration.
Verifies real-time 60s TWAP resolution from Polymarket RTDS feed.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch

from src.api.chainlink_twap import ChainlinkTWAPClient
from src.strategy.resolver import MarketResolver


class TestChainlinkTWAPClient:

    def test_record_and_get_twap_price(self):
        client = ChainlinkTWAPClient()
        client._history["BTC"].clear()

        # Record ticks at t=100, 101, 102
        client.record_tick("BTC", 100, 60000.0)
        client.record_tick("BTC", 101, 60050.0)
        client.record_tick("BTC", 102, 60100.0)

        # Exact match
        assert client.get_twap_price("BTC", 101, tolerance_sec=0) == 60050.0

        # Match within tolerance
        assert client.get_twap_price("BTC", 103, tolerance_sec=2) == 60100.0

        # Out of tolerance
        assert client.get_twap_price("BTC", 110, tolerance_sec=2) is None

        # Non-existent coin
        assert client.get_twap_price("SOL_UNKNOWN", 100) is None

    def test_deduplicate_same_timestamp_tick(self):
        client = ChainlinkTWAPClient()
        client._history["ETH"].clear()

        client.record_tick("ETH", 500, 3000.0)
        client.record_tick("ETH", 500, 3005.0)  # Overwrite same second

        assert len(client._history["ETH"]) == 1
        assert client.get_twap_price("ETH", 500) == 3005.0

    def test_resolution_outcomes(self):
        client = ChainlinkTWAPClient()
        client._history["BTC"].clear()

        # Start at 1000, End at 1300
        client.record_tick("BTC", 1000, 50000.0)
        client.record_tick("BTC", 1300, 50100.0)

        # Close >= Start -> UP
        res = client.get_resolution("BTC", 1000, 1300)
        assert res is not None
        outcome, p_start, p_close = res
        assert outcome == "UP"
        assert p_start == 50000.0
        assert p_close == 50100.0

        # Close < Start -> DOWN
        client.record_tick("BTC", 1600, 49900.0)
        res_down = client.get_resolution("BTC", 1300, 1600)
        assert res_down is not None
        outcome_down, p_start_down, p_close_down = res_down
        assert outcome_down == "DOWN"
        assert p_start_down == 50100.0
        assert p_close_down == 49900.0

        # Missing tick -> None
        assert client.get_resolution("BTC", 1000, 2000) is None

    def test_handle_rtds_message_parsing(self):
        client = ChainlinkTWAPClient()
        client._history["BTC"].clear()

        msg = {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "timestamp": 1773057112000,
            "payload": {
                "symbol": "btc/usd",
                "value": 89452.12,
                "timestamp": 1773057110000
            }
        }
        client._handle_message(json.dumps(msg))

        ts_sec = 1773057110
        assert client.get_twap_price("BTC", ts_sec) == 89452.12
        assert client.get_last_price("BTC") == 89452.12


class TestMarketResolverChainlinkTWAP:

    @pytest.mark.asyncio
    async def test_resolver_primary_chainlink_twap(self):
        resolver = MarketResolver()
        resolver.clear_cache()
        gamma_mock = AsyncMock()

        with patch.object(resolver, "_get_chainlink_twap_resolution", AsyncMock(return_value="UP")) as mock_twap:
            res_yes = await resolver.get_market_resolution(
                None, gamma_mock, "0xmarket1", "btc", 5, 1700000000, "YES"
            )
            assert res_yes == ("UP", 1.0)
            assert mock_twap.call_count == 1

            # Opposing side (NO loses on UP)
            res_no = await resolver.get_market_resolution(
                None, gamma_mock, "0xmarket1", "btc", 5, 1700000000, "NO"
            )
            assert res_no == ("UP", 0.0)

    @pytest.mark.asyncio
    async def test_resolver_fallback_to_gamma_when_twap_unavailable(self):
        resolver = MarketResolver()
        resolver.clear_cache()

        # Mock TWAP unavailable ("?")
        with patch.object(resolver, "_get_chainlink_twap_resolution", AsyncMock(return_value="?")):
            with patch.object(resolver, "_get_binance_resolution", AsyncMock(return_value="?")):
                gamma_mock = AsyncMock()
                gamma_mock._get = AsyncMock(return_value={
                    "outcomePrices": '["1", "0"]',
                    "outcomes": '["Yes", "No"]'
                })

                # Closes at 100s ago, now is 200
                res = await resolver.get_market_resolution(
                    None, gamma_mock, "0xmarket_real", "btc", 5, 0, "YES"
                )
                assert res == ("UP", 1.0)
