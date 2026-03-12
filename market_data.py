# Kalshi Market Data Fetcher

import base64
import datetime
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import BotConfig, KALSHI_WEATHER_SERIES

logger = logging.getLogger("kalshi_bot.markets")

def _sign_request(private_key, key_id: str, method: str, path: str) -> dict:
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    clean_path = path.split("?")[0]
    msg = f"{ts}{method}{clean_path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

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

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume: int
    open_interest: int
    close_time: Optional[datetime.datetime]
    series_ticker: str
    status: str
    order_book: Optional[OrderBook] = None

class KalshiClient:
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

    def __init__(self, config: BotConfig):
        self.config = config
        self.private_key = None
        self.key_id = config.kalshi.api_key_id

        key_str = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
        if key_str:
            try:
                if "BEGIN" not in key_str:
                    # wrap raw base64 in PEM headers
                    body = "\n".join(textwrap.wrap(key_str.replace("\\n", "").replace("\n", ""), 64))
                    key_str = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
                else:
                    key_str = key_str.replace("\\n", "\n")
                    key_str = re.sub(r'-----BEGIN RSA PRIVATE KEY-----\s*', '-----BEGIN RSA PRIVATE KEY-----\n', key_str)
                    key_str = re.sub(r'\s*-----END RSA PRIVATE KEY-----', '\n-----END RSA PRIVATE KEY-----', key_str)
                self.private_key = serialization.load_pem_private_key(key_str.encode(), password=None)
                logger.info("Loaded Kalshi private key from environment string")
            except Exception as e:
                logger.warning(f"Could not parse KALSHI_PRIVATE_KEY string: {e}")

        if self.private_key is None:
            pem_path = config.kalshi.private_key_path
            try:
                with open(pem_path, "rb") as f:
                    self.private_key = serialization.load_pem_private_key(f.read(), password=None)
                logger.info(f"Loaded Kalshi private key from file: {pem_path}")
            except Exception as e:
                logger.warning(f"Could not load private key: {e}  (dry run will still work)")

    def _headers(self, method: str, path: str) -> dict:
        if self.private_key is None:
            return {"Content-Type": "application/json"}
        return _sign_request(self.private_key, self.key_id, method, path)

    async def get_balance(self) -> float:
        if self.private_key is None:
            return 30.0
        path = "/portfolio/balance"
        async with httpx.AsyncClient() as client:
            r = await client.get(
                self.BASE_URL + path,
                headers=self._headers("GET", path),
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("balance", 0) / 100

    async def get_markets(self, series_ticker: str) -> List[KalshiMarket]:
        path = f"/markets"
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        query = f"{path}?series_ticker={series_ticker}&status=open&limit=200"
        async with httpx.AsyncClient() as client:
            r = await client.get(
                self.BASE_URL + query,
                headers=self._headers("GET", path),
                timeout=10,
            )
            r.raise_for_status()
            markets = []
            for m in r.json().get("markets", []):
                close_time = None
                if m.get("close_time"):
                    try:
                        close_time = datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                    except Exception:
                        pass
                markets.append(KalshiMarket(
                    ticker=m["ticker"],
                    title=m.get("title", ""),
                    yes_bid=m.get("yes_bid", 0) / 100,
                    yes_ask=m.get("yes_ask", 0) / 100,
                    no_bid=m.get("no_bid", 0) / 100,
                    no_ask=m.get("no_ask", 0) / 100,
                    volume=m.get("volume", 0),
                    open_interest=m.get("open_interest", 0),
                    close_time=close_time,
                    series_ticker=m.get("series_ticker", series_ticker),
                    status=m.get("status", ""),
                ))
            return markets

    async def place_order(self, ticker: str, side: str, count: int, price: int) -> dict:
        if self.config.trading.dry_run:
            logger.info(f"DRY RUN order: {side} {count}x {ticker} @ {price}c")
            return {"status": "dry_run"}
        path = "/portfolio/orders"
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            "yes_price": price if side == "yes" else 100 - price,
            "no_price": price if side == "no" else 100 - price,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self.BASE_URL + path,
                headers=self._headers("POST", path),
                json=body,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()

    async def get_positions(self) -> List[dict]:
        if self.private_key is None:
            return []
        path = "/portfolio/positions"
        async with httpx.AsyncClient() as client:
            r = await client.get(
                self.BASE_URL + path,
                headers=self._headers("GET", path),
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("market_positions", [])


class MarketDataFetcher:
    def __init__(self, config: BotConfig):
        self.config = config
        self.client = KalshiClient(config)

    async def fetch_all_weather_markets(self) -> List[KalshiMarket]:
        import asyncio
        tasks = [self.client.get_markets(s) for s in KALSHI_WEATHER_SERIES]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        markets = []
        for series, result in zip(KALSHI_WEATHER_SERIES, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch {series}: {result}")
            else:
                markets.extend(result)
        logger.info(f"Fetched {len(markets)} open Kalshi weather markets")
        return markets

    async def get_balance(self) -> float:
        return await self.client.get_balance()

    async def place_order(self, ticker: str, side: str, count: int, price: int) -> dict:
        return await self.client.place_order(ticker, side, count, price)

    async def get_positions(self) -> List[dict]:
        return await self.client.get_positions()
