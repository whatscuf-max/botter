# Kalshi Trade Executor
# Handles order placement and position management via the Kalshi REST API v2.
# Replaces the old py-clob-client based Polymarket executor.

import asyncio
import datetime
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import httpx

from config import BotConfig
from market_data import KalshiClient, Market

logger = logging.getLogger("kalshi_bot.executor")


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Side(Enum):
    YES = "yes"
    NO = "no"


@dataclass
class Order:
    order_id: str
    ticker: str
    side: Side
    count: int          # number of contracts (1 contract = $0.01 min, settles at $1)
    yes_price: int      # price in cents (1-99)
    status: OrderStatus = OrderStatus.PENDING
    filled_count: int = 0
    ts: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())


@dataclass
class Position:
    market_slug: str
    question: str
    side: Side
    contracts: int
    entry_price: float          # decimal
    current_price: float = 0.0
    pnl: float = 0.0
    entry_ts: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())
    forecast_temp: float = 0.0
    strike_temp: float = 0.0
    series_ticker: str = ""

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.contracts

    @property
    def age_seconds(self) -> float:
        try:
            dt = datetime.datetime.fromisoformat(self.entry_ts)
            return (datetime.datetime.utcnow() - dt).total_seconds()
        except Exception:
            return 0.0

    def update_pnl(self):
        self.pnl = (self.current_price - self.entry_price) * self.contracts


class TradeExecutor:
    def __init__(self, config: BotConfig):
        self.config = config
        self.dry_run = config.dry_run
        self.balance = config.trading.starting_balance
        self._client = KalshiClient(config)
        self.positions: Dict[str, Position] = {}
        self.order_history: List[Order] = []
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        market: Market,
        side: Side,
        contracts: int,
        yes_price_cents: int,
    ) -> Optional[Order]:
        order_id = str(uuid.uuid4())
        order = Order(
            order_id=order_id,
            ticker=market.slug,
            side=side,
            count=contracts,
            yes_price=yes_price_cents,
        )

        if self.dry_run:
            logger.info(
                f"[DRY RUN] BUY {contracts}x {market.slug} {side.value.upper()} "
                f"@ {yes_price_cents}c  |  Q: {market.question}"
            )
            order.status = OrderStatus.FILLED
            order.filled_count = contracts
            self._record_position(market, side, contracts, yes_price_cents / 100.0)
            self.order_history.append(order)
            return order

        # Live order
        body = {
            "ticker": market.slug,
            "action": "buy",
            "side": side.value,
            "type": "limit",
            "count": contracts,
            "yes_price": yes_price_cents,
            "client_order_id": order_id,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await self._client.post(http, "/portfolio/orders", body)
            kalshi_order = resp.get("order", {})
            status_str = kalshi_order.get("status", "")
            order.status = OrderStatus.FILLED if "filled" in status_str else OrderStatus.PENDING
            order.filled_count = kalshi_order.get("filled_count", 0)
            if order.filled_count > 0:
                self._record_position(market, side, order.filled_count, yes_price_cents / 100.0)
            self.order_history.append(order)
            logger.info(f"Order placed: {order.order_id} status={order.status.value}")
            return order
        except Exception as e:
            logger.error(f"Order failed for {market.slug}: {e}")
            order.status = OrderStatus.FAILED
            self.order_history.append(order)
            return order

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _record_position(self, market: Market, side: Side, contracts: int, price: float):
        pos = Position(
            market_slug=market.slug,
            question=market.question,
            side=side,
            contracts=contracts,
            entry_price=price,
            current_price=price,
            strike_temp=market.strike,
            series_ticker=market.series_ticker,
        )
        self.positions[market.slug] = pos
        cost = price * contracts
        self.balance -= cost
        logger.info(f"Position opened: {market.slug} {side.value} x{contracts} @ {price:.2f}  balance=${self.balance:.2f}")

    async def sell_position(self, market_slug: str, current_price: float) -> float:
        pos = self.positions.get(market_slug)
        if not pos:
            return 0.0

        pos.current_price = current_price
        pos.update_pnl()
        proceeds = current_price * pos.contracts

        if self.dry_run:
            logger.info(
                f"[DRY RUN] SELL {pos.contracts}x {market_slug} @ {current_price:.2f}  "
                f"PnL=${pos.pnl:+.2f}"
            )
        else:
            sell_side = Side.NO if pos.side == Side.YES else Side.YES
            sell_price_cents = int(current_price * 100)
            body = {
                "ticker": market_slug,
                "action": "sell",
                "side": pos.side.value,
                "type": "limit",
                "count": pos.contracts,
                "yes_price": sell_price_cents,
                "client_order_id": str(uuid.uuid4()),
            }
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    await self._client.post(http, "/portfolio/orders", body)
            except Exception as e:
                logger.error(f"Sell failed for {market_slug}: {e}")

        self.balance += proceeds
        self.daily_pnl += pos.pnl
        self.total_pnl += pos.pnl
        del self.positions[market_slug]
        return pos.pnl

    async def sell_all(self, market_prices: Dict[str, float]):
        for slug in list(self.positions.keys()):
            price = market_prices.get(slug, self.positions[slug].entry_price)
            await self.sell_position(slug, price)

    def update_position_prices(self, market_slug: str, current_price: float):
        if market_slug in self.positions:
            self.positions[market_slug].current_price = current_price
            self.positions[market_slug].update_pnl()

    def get_summary(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "open_positions": len(self.positions),
            "daily_pnl": round(self.daily_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "dry_run": self.dry_run,
        }
