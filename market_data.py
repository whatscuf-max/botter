# Kalshi Market Data Fetcher

import base64
import datetime
import logging
import re
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
        "KALSHI-ACCESS-KEY": "",
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

def _load_private_key_from_str(key_str: str):
    """Normalize \\n escapes and load a PEM private key from a string."""
    key_str = key_str.replace('\\\\n', '\n').replace('\\n', '\n')
    return serialization.load_pem_private_key(key_str.encode("utf-8"), password=None)

@dataclass
class OrderBookLevel:
    price: float
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

@dataclass
class MarketOutcome:
    token_id: str
    outcome: str
    price: float
    order_book: Optional[OrderBook] = None

@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    outcomes: List[MarketOutcome] = field(default_factory=list)
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: str = ""
    active: bool = True
    tags: List[str] = field(default_factory=list)
    series_ticker: str = ""
    strike: float = 0.0

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
        return (1.0 - cp) if cp is not None else None

# Alias for backwards compatibility
KalshiMarket = Market

class KalshiClient:
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.kalshi.active_url
        self.api_key_id = config.kalshi.api_key_id
        self._private_key = None
        # Only load key if we have a real key ID (not dry run placeholder)
        if config.kalshi.api_key_id:
            # First try the key string from .env
            key_str = getattr(config.kalshi, 'private_key_str', '') or ''
            if key_str.strip():
                try:
                    self._private_key = _load_private_key_from_str(key_str)
                    logger.info("Private key loaded from environment variable")
                except Exception as e:
                    logger.warning(f"Could not parse KALSHI_PRIVATE_KEY string: {e}")
            # Fall back to .pem file
            if self._private_key is None:
                try:
                    with open(config.kalshi.private_key_path, "rb") as f:
                        data = f.read()
                    if data.strip():
                        self._private_key = _load_private_key_from_str(data.decode("utf-8"))
                        logger.info("Private key loaded from .pem file")
                except Exception as e:
                    logger.warning(f"Could not load private key: {e}  (dry run will still work)")

    def _headers(self, method: str, path: str) -> dict:
        if self._private_key is None:
            return {"Content-Type": "application/json"}
        h = _sign_request(self._private_key, method, path)
        h["KALSHI-ACCESS-KEY"] = self.api_key_id
        return h

    async def get(self, client: httpx.AsyncClient, path: str, params: dict = None):
        url = self.base_url + path
        headers = self._headers("GET", "/trade-api/v2" + path)
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, client: httpx.AsyncClient, path: str, body: dict = None):
        url = self.base_url + path
        headers = self._headers("POST", "/trade-api/v2" + path)
        r = await client.post(url, headers=headers, json=body or {})
        r.raise_for_status()
        return r.json()

class MarketDataFetcher:
    def __init__(self, config: BotConfig):
        self.config = config
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

    async def fetch_market_with_books(self, market: Market) -> Market:
        async with httpx.AsyncClient(timeout=15) as http:
            try:
                data = await self.client.get(http, f"/markets/{market.slug}/orderbook")
                ob_raw = data.get("orderbook", {})
                bids = [OrderBookLevel(price=p / 100.0, size=s) for p, s in ob_raw.get("yes", [])]
                asks = [OrderBookLevel(price=p / 100.0, size=s) for p, s in ob_raw.get("no", [])]
                ob = OrderBook(bids=bids, asks=asks)
                for o in market.outcomes:
                    o.order_book = ob
            except Exception as e:
                logger.debug(f"Orderbook fetch failed for {market.slug}: {e}")
        return market

    def _parse_market(self, raw: dict, series_ticker: str) -> Optional[Market]:
        try:
            ticker = raw.get("ticker", "")
            yes_cents = raw.get("yes_bid", 0) or raw.get("last_price", 50)
            no_cents = 100 - yes_cents
            yes_price = yes_cents / 100.0
            no_price = no_cents / 100.0
            outcomes = [\
                MarketOutcome(token_id=ticker + "-YES", outcome="Yes", price=yes_price),\
                MarketOutcome(token_id=ticker + "-NO",  outcome="No",  price=no_price),\
            ]
            strike = 0.0
            title = raw.get("title", "") or raw.get("subtitle", "")
            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:degrees|deg|F)", title, re.IGNORECASE)
            if m:
                strike = float(m.group(1))
            return Market(
                condition_id=raw.get("event_ticker", series_ticker),
                question=title,
                slug=ticker,
                outcomes=outcomes,
                volume_24h=float(raw.get("volume", 0) or raw.get("volume_24h", 0) or 0),
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

    def record_price(self, token_id: str, price: float):
        if token_id not in self._price_history:
            self._price_history[token_id] = []
        self._price_history[token_id].append(price)
        if len(self._price_history[token_id]) > 500:
            self._price_history[token_id] = self._price_history[token_id][-500:]

    def get_price_history(self, token_id: str, lookback: int = 20) -> List[float]:
        return self._price_history.get(token_id, [])[-lookback:]

    async def close(self):
        pass
