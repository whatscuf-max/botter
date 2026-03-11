# Kalshi Market Data Fetcher
# Scans Kalshi weather temperature markets via the Kalshi REST API v2.
# Replaces the old Polymarket Gamma + CLOB fetcher.

import base64
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import BotConfig, KALSHI_WEATHER_SERIES

logger = logging.getLogger("kalshi_bot.markets")


def _sign_request(private_key, method: str, path: str) -> dict:
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    clean_path = path.split("?")[0]
    msg = f"{ts}{method}{clean_path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": "",   # filled in by KalshiClient
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


@dataclass
class OrderBookLevel:
    price: float   # converted from cents to decimal (e.g. 65 -> 0.65)
    size: int


@dataclass
class OrderBook:
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def bid_liquidity(self) -> float:
        return sum(b.price * b.size for b in self.bids)

    @property
    def ask_liquidity(self) -> float:
        return sum(a.price * a.size for a in self.asks)


@dataclass
class MarketOutcome:
    token_id: str       # Kalshi: market ticker (YES side)
    outcome: str        # "Yes" or "No"
    price: float        # decimal 0.01-0.99
    order_book: Optional[OrderBook] = None


@dataclass
class Market:
    condition_id: str       # Kalshi event ticker
    question: str
    slug: str               # Kalshi market ticker
    outcomes: List[MarketOutcome] = field(default_factory=list)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    active: bool = True
    tags: List[str] = field(default_factory=list)
    resolution_source: str = "NWS Daily Climatological Report"
    series_ticker: str = ""
    strike: float = 0.0     # temperature strike in degrees F

    @property
    def yes_price(self) -> Optional[float]:
        for o in self.outcomes:
            if o.outcome.lower() == "yes":
                return o.price
        return None

    @property
    def no_price(self) -> Optional[float]:
        for o in self.outcomes:
            if o.outcome.lower() == "no":
                return o.price
        return None

    @property
    def yes_token_id(self) -> Optional[str]:
        for o in self.outcomes:
            if o.outcome.lower() == "yes":
                return o.token_id
        return None

    @property
    def combined_price(self) -> Optional[float]:
        y, n = self.yes_price, self.no_price
        if y is not None and n is not None:
            return y + n
        return None

    @property
    def arb_spread(self) -> Optional[float]:
        cp = self.combined_price
        if cp is not None:
            return 1.0 - cp
        return None


class KalshiClient:
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.kalshi.active_url
        self.api_key_id = config.kalshi.api_key_id
        with open(config.kalshi.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _headers(self, method: str, path: str) -> dict:
        h = _sign_request(self._private_key, method, path)
        h["KALSHI-ACCESS-KEY"] = self.api_key_id
        return h

    async def get(self, client: httpx.AsyncClient, path: str, params: dict = None):
        url = self.base_url + path
        headers = self._headers("GET", path)
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, client: httpx.AsyncClient, path: str, body: dict = None):
        url = self.base_url + path
        headers = self._headers("POST", path)
        r = await client.post(url, headers=headers, json=body or {})
        r.raise_for_status()
        return r.json()

    async def delete(self, client: httpx.AsyncClient, path: str, body: dict = None):
        url = self.base_url + path
        headers = self._headers("DELETE", path)
        r = await client.request("DELETE", url, headers=headers, json=body or {})
        r.raise_for_status()
        return r.json()


class MarketDataFetcher:
    def __init__(self, config: BotConfig):
        self.client = KalshiClient(config)
        self._price_history: Dict[str, List[float]] = {}

    async def fetch_active_markets(self, limit: int = 100) -> List[Market]:
        markets = []
        async with httpx.AsyncClient(timeout=30) as http:
            for series_ticker, info in KALSHI_WEATHER_SERIES.items():
                try:
                    data = await self.client.get(
                        http,
                        "/markets",
                        params={"series_ticker": series_ticker, "status": "open", "limit": limit},
                    )
                    for m in data.get("markets", []):
                        market = self._parse_market(m, series_ticker)
                        if market:
                            markets.append(market)
                except Exception as e:
                    logger.warning(f"Failed to fetch {series_ticker}: {e}")
        logger.info(f"Fetched {len(markets)} open Kalshi weather markets")
        return markets

    def _parse_market(self, raw: dict, series_ticker: str) -> Optional[Market]:
        try:
            ticker = raw.get("ticker", "")
            yes_cents = raw.get("yes_bid", 0) or raw.get("last_price", 50)
            no_cents = 100 - yes_cents
            yes_price = yes_cents / 100.0
            no_price = no_cents / 100.0

            outcomes = [
                MarketOutcome(token_id=ticker + "-YES", outcome="Yes", price=yes_price),
                MarketOutcome(token_id=ticker + "-NO",  outcome="No",  price=no_price),
            ]

            # Parse strike from subtitle or title e.g. "above 72 degrees"
            strike = 0.0
            title = raw.get("title", "") or raw.get("subtitle", "")
            import re
            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:degrees|deg|F)", title, re.IGNORECASE)
            if m:
                strike = float(m.group(1))

            return Market(
                condition_id=raw.get("event_ticker", series_ticker),
                question=title,
                slug=ticker,
                outcomes=outcomes,
                volume_24h=float(raw.get("volume_24h", 0) or 0),
                liquidity=float(raw.get("liquidity", 0) or 0),
                end_date=raw.get("close_time", ""),
                active=raw.get("status", "") == "open",
                tags=["weather", "temperature"],
                series_ticker=series_ticker,
                strike=strike,
            )
        except Exception as e:
            logger.debug(f"Failed to parse market {raw.get('ticker')}: {e}")
            return None

    async def fetch_market_orderbook(self, market: Market) -> Market:
        async with httpx.AsyncClient(timeout=15) as http:
            try:
                data = await self.client.get(http, f"/markets/{market.slug}/orderbook")
                ob_raw = data.get("orderbook", {})
                bids = [OrderBookLevel(price=p/100.0, size=s) for p, s in ob_raw.get("yes", [])]
                asks = [OrderBookLevel(price=p/100.0, size=s) for p, s in ob_raw.get("no", [])]
                ob = OrderBook(bids=bids, asks=asks)
                for o in market.outcomes:
                    o.order_book = ob
            except Exception as e:
                logger.debug(f"Orderbook fetch failed for {market.slug}: {e}")
        return market

    def record_price(self, token_id: str, price: float):
        if token_id not in self._price_history:
            self._price_history[token_id] = []
        self._price_history[token_id].append(price)
        if len(self._price_history[token_id]) > 500:
            self._price_history[token_id] = self._price_history[token_id][-500:]

    def get_price_history(self, token_id: str, lookback: int = 20) -> List[float]:
        return self._price_history.get(token_id, [])[-lookback:]
