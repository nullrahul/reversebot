"""
Gamma API Client — Market Discovery & Polling for Polymarket.

Polls the Gamma API for active binary markets.
Parses market fields and returns structured data.

╔══════════════════════════════════════════════════════════════════════════╗
║ CRITICAL: VOLUME DOUBLE-COUNTING FIX                                    ║
║                                                                          ║
║ The Polymarket smart contract emits TWO OrderFilled events per trade     ║
║ (one for maker, one for taker). Any system summing all events shows      ║
║ exactly 2× the real volume.                                              ║
║                                                                          ║
║ This causes catastrophic position sizing errors — the bot thinks         ║
║ markets are twice as liquid as they actually are.                         ║
║                                                                          ║
║ RULE: Use TAKER-SIDE VOLUME ONLY. Filter by maker=false or the          ║
║ equivalent field in the API response.                                    ║
║                                                                          ║
║ Unit test: test_scanner.py::test_volume_is_taker_side_only               ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiohttp

from src.api.rate_limiter import RateLimiter
from src.logs.log_config import get_logger

logger = get_logger("gamma_client")

SHORT_WINDOW_TAG_TO_MINUTES = {
    "5m": 5,
    "15m": 15,
}


def _normalize_tag_slug(tag: Dict[str, Any]) -> str:
    return str(tag.get("slug", "")).strip().lower()


def _parse_timestamp(value: Any) -> Optional[int]:
    """Parse ISO8601 strings or unix timestamps into epoch seconds."""
    if value in (None, ""):
        return None

    try:
        from datetime import datetime

        if isinstance(value, str):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        if isinstance(value, (int, float)):
            return int(value)
    except (TypeError, ValueError):
        return None

    return None


class GammaClient:
    """
    Async client for the Polymarket Gamma API.
    Handles market discovery, polling, and data normalization.
    """

    def __init__(
        self,
        gamma_api_url: str = "https://gamma-api.polymarket.com",
        rate_limiter: Optional[RateLimiter] = None,
    ):
        self._base_url = gamma_api_url.rstrip("/")
        self._rate_limiter = rate_limiter
        self._session: Optional[aiohttp.ClientSession] = None

        logger.info(
            "gamma_client_initialized",
            base_url=self._base_url,
            message="Gamma API client ready",
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Lazy-init aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """Make a rate-limited GET request to the Gamma API."""
        if self._rate_limiter:
            await self._rate_limiter.acquire_api_call()

        session = await self._ensure_session()
        url = f"{self._base_url}{path}"

        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(
                        "gamma_api_error",
                        path=path,
                        status=resp.status,
                        response=text[:500],
                        message=f"Gamma API error: {resp.status}",
                    )
                    return None
        except aiohttp.ClientError as e:
            logger.error(
                "gamma_api_connection_error",
                path=path,
                error=str(e),
                message=f"Gamma API connection error: {e}",
            )
            return None

    async def get_true_now(self) -> float:
        """Get real world UTC time from API headers."""
        try:
            from email.utils import parsedate_to_datetime
            async with self.session.get(f"{self.base_url}/markets?limit=1") as resp:
                date_str = resp.headers.get("Date")
                if date_str:
                    return parsedate_to_datetime(date_str).timestamp()
        except: pass
        return time.time()

    async def fetch_ephemeral_crypto_markets(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """
        Global Brute Force Search for 5m/15m crypto windows.
        Scans 5,000 markets and ignores title formatting errors.
        """
        matches_dict = {}
        coins = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "doge", "bnb", "hyperliquid", "hype", "xrp", "ton", "ada", "pepe"]
        
        # Paged Discovery
        for page in range(5):
            offset = page * 1000
            params = {
                "limit": 1000,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "createdAt",
                "ascending": "false"
            }
            
            data = await self._get("/markets", params=params)
            if not data or not isinstance(data, list):
                break
                
            for raw in data:
                if not isinstance(raw, dict): continue
                q = raw.get("question", "").lower()
                
                # Broad Match: Must be crypto + must have a hyphen (window indicator)
                if any(coin in q for coin in coins) and "-" in q:
                    parsed = self._parse_market(raw)
                    if parsed:
                        mid = parsed.get("market_id")
                        if mid and mid not in matches_dict:
                            matches_dict[mid] = parsed
        
        matches = list(matches_dict.values())
        logger.info("global_discovery_complete", count=len(matches))
        return matches

    async def fetch_active_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        closed: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch active binary markets from Gamma API.

        Returns a list of normalized market dicts.
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
            
        if search:
            params["search"] = search

        data = await self._get("/events", params=params)

        if not data or not isinstance(data, list):
            return []

        markets = []
        for event in data:
            event_markets = event.get("markets", [])
            for raw_market in event_markets:
                # Inject event-level fields as fallbacks
                if "category" not in raw_market and "tags" in event:
                    raw_market["category"] = ", ".join([t.get("label", "") for t in event["tags"]])
                if "event_slug" not in raw_market:
                    raw_market["event_slug"] = event.get("slug", "")
                if "event_title" not in raw_market:
                    raw_market["event_title"] = event.get("title", "")
                if "event_id" not in raw_market:
                    raw_market["event_id"] = event.get("id")
                if "event_tags" not in raw_market:
                    raw_market["event_tags"] = event.get("tags", [])
                
                parsed = self._parse_market(raw_market)
                if parsed:
                    markets.append(parsed)

        logger.debug(
            "markets_fetched",
            count=len(markets),
            offset=offset,
            search=search,
            message=f"Fetched {len(markets)} markets from Gamma API",
        )

        return markets

    async def fetch_active_markets_index(
        self,
        limit: int = 100,
        offset: int = 0,
        closed: bool = False,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch active markets directly from Gamma `/markets`.

        Notes:
        - `/events` does not reliably support `search` (it may be ignored), and returns
          event objects with embedded markets.
        - `/markets` returns market objects and is better suited for slug/text search.
        """
        params = {
            "limit": limit,
            "offset": offset,
            "closed": str(closed).lower(),
            "active": "true",
        }
        if search:
            params["search"] = search

        data = await self._get("/markets", params=params)
        if not data or not isinstance(data, list):
            return []

        markets: List[Dict[str, Any]] = []
        for raw_market in data:
            parsed = self._parse_market(raw_market)
            if parsed:
                markets.append(parsed)

        logger.debug(
            "markets_index_fetched",
            count=len(markets),
            offset=offset,
            search=search,
            message=f"Fetched {len(markets)} markets from Gamma /markets",
        )

        return markets

    async def fetch_all_active_markets_index(
        self,
        max_pages: int = 20,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Paginate through active markets from `/markets`."""
        unique_markets: Dict[str, Dict[str, Any]] = {}
        offset = 0
        limit = 100
        page = 0

        while page < max_pages:
            batch = await self.fetch_active_markets_index(
                limit=limit,
                offset=offset,
                search=search,
            )
            if not batch:
                break

            for m in batch:
                unique_markets[m["market_id"]] = m

            offset += limit
            page += 1

        all_markets = list(unique_markets.values())
        logger.info(
            "all_markets_index_fetched",
            total=len(all_markets),
            pages=page,
            search=search,
            message=f"Fetched {len(all_markets)} total markets from /markets",
        )

        return all_markets

    async def fetch_all_active_markets(
        self,
        max_pages: int = 20,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Paginate through active binary markets.
        Increased to 20 pages to ensure we catch all new crypto short-windows.
        """
        unique_markets = {}
        offset = 0
        limit = 100
        page = 0

        while page < max_pages:
            batch = await self.fetch_active_markets(
                limit=limit,
                offset=offset,
                search=search,
            )
            
            if not batch:
                break
                
            for m in batch:
                unique_markets[m["market_id"]] = m

            offset += limit
            page += 1

        all_markets = list(unique_markets.values())

        logger.info(
            "all_markets_fetched",
            total=len(all_markets),
            pages=page,
            search=search,
            message=f"Fetched {len(all_markets)} active markets across {page} pages.",
        )

        return all_markets

    def _parse_market(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse a raw Gamma API market response into our internal format.
        Extracts only binary markets and normalizes fields.

        CRITICAL: Volume uses taker-side only to avoid double-counting.
        """
        try:
            # Parse stringified JSON arrays
            import json
            
            raw_outcomes = raw.get("outcomes", "[]")
            if isinstance(raw_outcomes, str):
                try:
                    outcomes = json.loads(raw_outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            else:
                outcomes = raw_outcomes

            if not outcomes or len(outcomes) != 2:
                return None

            market_id = raw.get("id") or raw.get("condition_id")
            if not market_id:
                return None

            # Extract prices
            raw_prices = raw.get("outcomePrices", "[]")
            if isinstance(raw_prices, str):
                try:
                    prices = json.loads(raw_prices)
                except json.JSONDecodeError:
                    prices = []
            else:
                prices = raw_prices

            yes_price = None
            no_price = None

            if len(prices) == 2:
                for outcome, price in zip(outcomes, prices):
                    try:
                        p = float(price)
                        if outcome.upper() == "YES":
                            yes_price = p
                        elif outcome.upper() == "NO":
                            no_price = p
                    except (ValueError, TypeError):
                        pass

            # If prices are completely missing from the API (e.g. brand new market), default to 0.5
            if yes_price is None or no_price is None:
                yes_price = 0.5
                no_price = 0.5

            # ───────────────────────────────────────────────────────
            # VOLUME: TAKER-SIDE ONLY
            # The raw volume field counts BOTH maker and taker fills.
            # We halve it to get single-sided (taker) volume.
            # This is the documented fix for the 2× volume bug.
            # ───────────────────────────────────────────────────────
            # Use volume24hr if available, else derive from volume1wk
            raw_volume_24h = float(raw.get("volume24hr", raw.get("volume_24h", 0)) or 0)
            if not raw_volume_24h and raw.get("volume1wkNum"):
                raw_volume_24h = float(raw.get("volume1wkNum")) / 7
            elif not raw_volume_24h and raw.get("volume1wk"):
                raw_volume_24h = float(raw.get("volume1wk")) / 7

            raw_total_volume = float(raw.get("volumeNum", raw.get("volume", 0)) or 0)

            taker_volume_24h = raw_volume_24h / 2.0
            taker_total_volume = raw_total_volume / 2.0

            event_tags = raw.get("event_tags", [])
            if not isinstance(event_tags, list):
                event_tags = []

            tag_slugs = [_normalize_tag_slug(tag) for tag in event_tags]
            tag_labels = [str(tag.get("label", "")).strip() for tag in event_tags]
            tag_ids = [str(tag.get("id")) for tag in event_tags if tag.get("id") is not None]

            # For recurring updown markets, startDate is creation/activation time, not the trading window start.
            starts_at_ts = _parse_timestamp(raw.get("gameStartTime"))
            closes_at_ts = _parse_timestamp(
                raw.get("endDateIso")
                or raw.get("endDate")
                or raw.get("end_date_iso")
            )
            window_minutes = None
            if starts_at_ts and closes_at_ts and closes_at_ts > starts_at_ts:
                window_minutes = int(round((closes_at_ts - starts_at_ts) / 60.0))
            if window_minutes is None:
                for slug in tag_slugs:
                    if slug in SHORT_WINDOW_TAG_TO_MINUTES:
                        window_minutes = SHORT_WINDOW_TAG_TO_MINUTES[slug]
                        break
            if window_minutes is None:
                market_slug = str(raw.get("slug", "")).lower()
                if "-updown-5m-" in market_slug:
                    window_minutes = 5
                elif "-updown-15m-" in market_slug:
                    window_minutes = 15
            if starts_at_ts is None and closes_at_ts and window_minutes:
                starts_at_ts = closes_at_ts - (window_minutes * 60)

            yes_token_id = None
            no_token_id = None
            raw_token_ids = raw.get("clobTokenIds", "[]")
            if isinstance(raw_token_ids, str):
                try:
                    token_ids = json.loads(raw_token_ids)
                    if len(token_ids) >= 2:
                        yes_token_id = str(token_ids[0])
                        no_token_id = str(token_ids[1])
                except Exception:
                    pass

            return {
                "market_id": str(market_id),
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "question": raw.get("question", ""),
                "slug": raw.get("slug", ""),
                "event_id": str(raw.get("event_id", "")),
                "event_slug": raw.get("event_slug", ""),
                "event_title": raw.get("event_title", ""),
                "event_tag_ids": tag_ids,
                "event_tag_slugs": tag_slugs,
                "event_tag_labels": tag_labels,
                "category": raw.get("category", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_volume_24h": taker_volume_24h,
                "total_volume": taker_total_volume,
                "resolution_source": raw.get("resolution_source", ""),
                "starts_at": starts_at_ts,
                "closes_at": closes_at_ts,
                "window_minutes": window_minutes,
                "accepting_orders": raw.get("acceptingOrders"),
                "order_min_size": float(raw.get("orderMinSize", 5.0)),
                "last_scanned_at": int(time.time()),
                "is_flagged": 0,
                "flag_reason": None,
                "last_seen_above_threshold": None,
                "consecutive_cycles_above": 0,
            }

        except (KeyError, TypeError, ValueError) as e:
            logger.warning(
                "market_parse_error",
                market_id=raw.get("id", "unknown"),
                error=str(e),
                message=f"Failed to parse market: {e}",
            )
            return None
