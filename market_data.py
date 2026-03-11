"""
Market Data Fetcher - fetches from Polymarket Gamma API and CLOB API.
Sorts by volume, supports pagination with offset to find weather markets.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("polymarket_bot.markets")


@dataclass
class OrderBookLevel:
    price: float
    size: float


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
    resolution_source: str = ""

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
    def no_token_id(self) -> Optional[str]:
        for o in self.outcomes:
            if o.outcome.lower() == "no":
                return o.token_id
        return None

    @property
    def combined_price(self) -> Optional[float]:
        if self.yes_price is not None and self.no_price is not None:
            return self.yes_price + self.no_price
        return None

    @property
    def arb_spread(self) -> Optional[float]:
        cp = self.combined_price
        if cp is not None:
            return 1.0 - cp
        return None


class MarketDataFetcher:
    def __init__(self, gamma_host: str, clob_host: str):
        self.gamma_host = gamma_host
        self.clob_host = clob_host
        self._client = httpx.AsyncClient(timeout=30.0)
        self._market_cache: Dict[str, Market] = {}
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}

    async def close(self):
        await self._client.aclose()

    def _parse_gamma_market(self, raw: dict) -> Optional[Market]:
        outcomes_raw = raw.get("outcomes", "[]")
        prices_raw = raw.get("outcomePrices", "[]")
        token_ids_raw = raw.get("clobTokenIds", "[]")

        try:
            outcome_names = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (json.JSONDecodeError, TypeError):
            outcome_names = []

        try:
            prices_list = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            outcome_prices = [float(p) for p in prices_list]
        except (json.JSONDecodeError, TypeError, ValueError):
            outcome_prices = []

        try:
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        if not outcome_names or not outcome_prices:
            return None

        outcomes = []
        for i, name in enumerate(outcome_names):
            price = outcome_prices[i] if i < len(outcome_prices) else 0.0
            token_id = token_ids[i] if i < len(token_ids) else ""
            outcomes.append(MarketOutcome(
                token_id=str(token_id), outcome=str(name), price=price,
            ))

        volume_24h = 0.0
        for f in ["volume24hr", "volume24hrClob"]:
            v = raw.get(f)
            if v is not None:
                try:
                    volume_24h = float(v)
                    break
                except (ValueError, TypeError):
                    pass

        liquidity = 0.0
        for f in ["liquidityNum", "liquidityClob", "liquidity"]:
            v = raw.get(f)
            if v is not None:
                try:
                    liquidity = float(v)
                    break
                except (ValueError, TypeError):
                    pass

        end_date = raw.get("endDateIso", "") or raw.get("endDate", "") or ""

        return Market(
            condition_id=raw.get("conditionId", ""),
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            outcomes=outcomes,
            volume_24h=volume_24h,
            liquidity=liquidity,
            end_date=end_date,
            active=raw.get("active", True),
        )

    def record_price(self, token_id: str, price: float):
        now = time.time()
        if token_id not in self._price_history:
            self._price_history[token_id] = []
        self._price_history[token_id].append((now, price))
        if len(self._price_history[token_id]) > 500:
            self._price_history[token_id] = self._price_history[token_id][-500:]

    def get_price_history(self, token_id: str, lookback: int = 50) -> List[float]:
        history = self._price_history.get(token_id, [])
        return [p for _, p in history[-lookback:]]
