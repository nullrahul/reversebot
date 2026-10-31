import pytest
import json
import time
from unittest.mock import AsyncMock, MagicMock
import aiohttp
from src.api.websocket import WebSocketClient

class AsyncMsgIterator:
    def __init__(self, client, items):
        self.client = client
        self.items = items
        self.idx = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.idx < len(self.items):
            item = self.items[self.idx]
            self.idx += 1
            return item
        else:
            self.client._should_run = False  # Terminate the listen loop safely
            raise StopAsyncIteration

class MockMsg:
    def __init__(self, data):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(data)


@pytest.mark.asyncio
async def test_websocket_parsing_order_fix():
    # 1. Instantiate the client in local test mode
    client = WebSocketClient("wss://dummy")
    client._should_run = True
    client._connected = True
    
    token_id = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    
    # 2. Define events in sequence
    # Snapshot: YES token has bid at 0.50, ask at 0.55
    snapshot_event = {
        "event_type": "book",
        "asset_id": token_id,
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}]
    }
    
    # L2 Delta Update: new ask level added at 0.56 with size 200 (nested list)
    delta_add_event = {
        "event_type": "price_change",
        "market": "0xCONDITION_ID",
        "price_changes": [
            {
                "asset_id": token_id,
                "price": "0.56",
                "side": "SELL",
                "size": "200",
                "best_bid": "0.50",
                "best_ask": "0.56"
            }
        ]
    }
    
    # L2 Delta Update: ask level 0.55 depleted (size 0) (nested list)
    delta_delete_event = {
        "event_type": "price_change",
        "market": "0xCONDITION_ID",
        "price_changes": [
            {
                "asset_id": token_id,
                "price": "0.55",
                "side": "SELL",
                "size": "0",
                "best_bid": "0.50",
                "best_ask": "0.56"
            }
        ]
    }
    
    # Fallback Event: general price update from a non-L2 message type
    fallback_event = {
        "event_type": "general_trade",
        "asset_id": token_id,
        "price": "0.57"
    }
    
    messages = [
        MockMsg(snapshot_event),
        MockMsg(delta_add_event),
        MockMsg(delta_delete_event),
        MockMsg(fallback_event)
    ]
    
    client._ws = AsyncMsgIterator(client, messages)
    client.connect = AsyncMock(return_value=True)
    
    # 3. Execute the listen loop
    await client._listen_loop()
    
    # 4. Verify book replica states
    assert token_id in client.book_cache
    
    # Verify the book_cache state after all delta operations
    # Bid 0.50 should be intact
    assert 0.50 in client.book_cache[token_id]["bids"]
    assert client.book_cache[token_id]["bids"][0.50] == 100.0
    
    # Ask 0.55 should be depleted/deleted
    assert 0.55 not in client.book_cache[token_id]["asks"]
    
    # Ask 0.56 should be present with size 200
    assert 0.56 in client.book_cache[token_id]["asks"]
    assert client.book_cache[token_id]["asks"][0.56] == 200.0
    
    # Verify the derived cached prices
    # Best bid is 0.50
    assert client.bid_cache.get(token_id) == 0.50
    
    # Best ask is 0.56 (since 0.55 was deleted)
    assert client.ask_cache.get(token_id) == 0.56
    
    # Fallback price cache should have captured the general trade price of 0.57
    assert client.price_cache.get(token_id) == 0.57

    # 5. Verify robust public helper methods resolve correctly
    assert client.get_best_bid(token_id) == 0.50
    assert client.get_best_ask(token_id) == 0.56


@pytest.mark.asyncio
async def test_websocket_incremental_subscription():
    client = WebSocketClient("wss://dummy")
    client._connected = True
    
    # Mock WS send_json method
    client._ws = AsyncMock()
    client._ws.closed = False
    
    token1 = "0xTOKEN_ONE_52391039867086884693717208573215579978712680486847249673328"
    token2 = "0xTOKEN_TWO_52391039867086884693717208573215579978712680486847249673328"
    
    # 1. Subscribe to first market
    await client.subscribe_market("market_1", yes_token_id=token1, push_state=True)
    
    # Verify send_json was called with token1
    client._ws.send_json.assert_called_once()
    payload1 = client._ws.send_json.call_args[0][0]
    assert payload1["assets_ids"] == [token1]
    assert token1 in client._subscribed_tokens
    
    client._ws.send_json.reset_mock()
    
    # 2. Subscribe to second market (incremental)
    await client.subscribe_market("market_2", yes_token_id=token2, push_state=True)
    
    # Verify send_json was called ONLY with token2
    client._ws.send_json.assert_called_once()
    payload2 = client._ws.send_json.call_args[0][0]
    assert payload2["assets_ids"] == [token2]
    assert token2 in client._subscribed_tokens
    
    client._ws.send_json.reset_mock()
    
    # 3. Force resubscribe (reconnect path)
    await client._resubscribe()
    
    # Verify it unsubscribes and sends subscribe payload for both tokens
    # Because force=True clears self._subscribed_tokens and subscribes to self._active_tokens
    assert client._ws.send_json.call_count >= 1
    # We should have both tokens in self._subscribed_tokens
    assert token1 in client._subscribed_tokens
    assert token2 in client._subscribed_tokens


@pytest.mark.asyncio
async def test_websocket_explicit_unsubscribe():
    client = WebSocketClient("wss://dummy")
    client._connected = True
    client._ws = AsyncMock()
    client._ws.closed = False
    
    token1 = "0xTOKEN_ONE_52391039867086884693717208573215579978712680486847249673328"
    token2 = "0xTOKEN_TWO_52391039867086884693717208573215579978712680486847249673328"
    
    # Pre-subscribe to tokens
    client._active_tokens.add(token1)
    client._active_tokens.add(token2)
    client._subscribed_tokens.add(token1)
    client._subscribed_tokens.add(token2)
    
    # Initialize some caches
    client.price_cache[token1] = 0.50
    client.price_cache[token2] = 0.60
    
    # Unsubscribe from market_1 (token1)
    await client.unsubscribe_market("market_1", yes_token_id=token1, push_state=False)
    
    # Verify local caches are cleared
    assert token1 not in client.price_cache
    assert token2 in client.price_cache
    
    # Verify it sent an explicit unsubscribe message to the WS server
    client._ws.send_json.assert_called_once()
    payload = client._ws.send_json.call_args[0][0]
    assert payload["operation"] == "unsubscribe"
    assert payload["assets_ids"] == [token1]
    
    # Verify local sets are updated
    assert token1 not in client._active_tokens
    assert token1 not in client._subscribed_tokens
    assert token2 in client._active_tokens
    assert token2 in client._subscribed_tokens


@pytest.mark.asyncio
async def test_get_best_ask_no_price_cache_fallback():
    client = WebSocketClient("wss://dummy")
    token_id = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    
    # Pre-populate only the general price cache (representing a bid price update)
    client.price_cache[token_id] = 0.17
    
    # Assert book_cache and ask_cache are empty for token
    assert token_id not in client.book_cache
    assert token_id not in client.ask_cache
    
    # get_best_ask() must NOT fall back to price_cache and should return None
    assert client.get_best_ask(token_id) is None
    
    # get_best_bid() is allowed to fall back to price_cache and should return 0.17
    assert client.get_best_bid(token_id) == 0.17


@pytest.mark.asyncio
async def test_websocket_unsubscribe_via_token_to_market_and_fast_abort():
    client = WebSocketClient("wss://dummy")
    client._connected = True
    client._ws = AsyncMock()
    client._ws.closed = False

    yes_t = "0xYES_TOKEN_111"
    no_t = "0xNO_TOKEN_222"
    await client.subscribe_market("market_auto", yes_token_id=yes_t, no_token_id=no_t, push_state=True)
    assert yes_t in client._active_tokens
    assert no_t in client._active_tokens
    assert yes_t in client._subscribed_tokens
    assert no_t in client._subscribed_tokens

    # Unsubscribe without passing explicit token IDs — must resolve via token_to_market
    client._ws.send_json.reset_mock()
    await client.unsubscribe_market("market_auto", push_state=False)
    assert yes_t not in client._active_tokens
    assert no_t not in client._active_tokens
    assert yes_t not in client._subscribed_tokens
    assert no_t not in client._subscribed_tokens
    assert "market_auto" not in client._subscribed_markets

    # Fast-abort test: 150 tokens (3 batches of 50), socket fails on first send_json
    for idx in range(150):
        client._active_tokens.add(f"0xTOK_{idx}")
    client._ws.send_json.reset_mock()
    client._ws.send_json.side_effect = RuntimeError("Cannot write to closing transport")

    await client._push_subscription_state(force=True)
    # Must abort on first failed batch rather than looping through all 3 batches
    assert client._ws.send_json.call_count == 1


@pytest.mark.asyncio
async def test_scanner_discovery_loop_unsubscribes_expired_markets_after_closes_at():
    import asyncio
    import tempfile
    import os
    from src.db.queries import init_db, close_db
    from src.strategy.scanner import MarketScanner

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db = await init_db(tmp.name)

    try:
        config = MagicMock()
        config.paper_trade = True
        config.target_crypto_symbols = ["BTC"]

        gamma = AsyncMock()
        clob = MagicMock()
        ws = WebSocketClient("wss://dummy")
        ws._connected = True
        ws._ws = AsyncMock()
        ws._ws.closed = False

        scanner = MarketScanner(config, gamma, ws, clob, db)
        scanner._verify_token_mapping = AsyncMock(
            return_value={"yes_token_id": "yes_tok_1", "no_token_id": "no_tok_1"}
        )

        market_1 = {
            "market_id": "m_exp_test",
            "question": "Bitcoin Up or Down - 5 Minutes",
            "slug": "btc-updown-5m-900",
            "accepting_orders": True,
            "active": True,
            "window_minutes": 5,
            "closes_at": 1000,
            "yes_token_id": "yes_tok_1",
            "no_token_id": "no_tok_1",
            "yes_price": 0.50,
            "no_price": 0.50,
        }

        # Sequence of true_now timestamps across 5 discovery cycles:
        # Cycle 1: t=900 (100s before close -> valid, subscribed)
        # Cycle 2: t=950 (50s before close -> drops from tracked_markets, dead_count=1, stays on WS)
        # Cycle 3: t=960 (40s before close -> dead_count=2, stays on WS)
        # Cycle 4: t=970 (30s before close -> dead_count=3, still <= closes_at=1000 so stays on WS!)
        # Cycle 5: t=1005 (5s after close -> dead_count=4 and > closes_at -> unsubscribed from WS!)
        now_sequence = [900, 950, 960, 970, 1005]
        states_after_cycle = []

        idx = 0

        async def fake_get_true_now():
            nonlocal idx
            return now_sequence[idx]

        gamma.get_true_now = AsyncMock(side_effect=fake_get_true_now)
        gamma.fetch_ephemeral_crypto_markets = AsyncMock(return_value=[market_1])

        orig_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            nonlocal idx
            if seconds == 10:
                states_after_cycle.append(
                    (
                        now_sequence[idx],
                        set(scanner.tracked_markets.keys()),
                        set(ws._active_tokens),
                    )
                )
                idx += 1
                if idx >= len(now_sequence):
                    scanner._should_run_discovery = False
            else:
                await orig_sleep(0)

        from unittest.mock import patch
        scanner._should_run_discovery = True
        with patch("src.strategy.scanner.asyncio.sleep", side_effect=fake_sleep):
            await scanner._discovery_loop()

        # Cycle 1 (t=900): tracked and subscribed
        assert states_after_cycle[0] == (900, {"m_exp_test"}, {"yes_tok_1", "no_tok_1"})
        # Cycle 2..4 (t=950..970): dropped from tracked_markets (<60s left), but WS tokens kept alive until closes_at!
        assert states_after_cycle[1] == (950, set(), {"yes_tok_1", "no_tok_1"})
        assert states_after_cycle[2] == (960, set(), {"yes_tok_1", "no_tok_1"})
        assert states_after_cycle[3] == (970, set(), {"yes_tok_1", "no_tok_1"})
        # Cycle 5 (t=1005 > closes_at): WS tokens cleanly unsubscribed, 0 leaked tokens!
        assert states_after_cycle[4] == (1005, set(), set())
    finally:
        await close_db(db)
        os.unlink(tmp.name)
