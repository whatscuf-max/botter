# Kalshi Trade Executor

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
    PENDING   = "pending"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    FAILED    = "failed"


class Side(Enum):
    YES = "yes"
    NO  = "no"


@dataclass
class Order:
    order_id: str
    ticker: str
    side: Side
    count: int
    yes_price: int
    status: OrderStatus = OrderStatus.PENDING
    filled_count: int = 0
    ts: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())


@dataclass
class Position:
    market_slug: str
    question: str
    token_id: str
    side: Side
    size: float
    entry_price: float
    current_price: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    entry_ts: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())
    strike_temp: float = 0.0
    series_ticker: str = ""

    @property
    def age_seconds(self) -> float:
        try:
            dt = datetime.datetime.fromisoformat(self.entry_ts)
            return (datetime.datetime.utcnow() - dt).total_seconds()
        except Exception:
            return 0.0

    def update_pnl(self, current_price: float = None):
        p = current_price if current_price is not None else self.current_price
        self.pnl = (p - self.entry_price) * self.size


class TradeExecutor:
    def __init__(self, config: BotConfig):
        self.config = config
        self.dry_run = config.dry_run
        self.balance = config.trading.starting_balance
        self._client = KalshiClient(config)
        self.open_positions: Dict[str, Position] = {}
        self.trade_history: List[Position] = []
        self.order_history: List[Order] = []
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self._prices: Dict[str, float] = {}

    @property
    def available_balance(self) -> float:
        invested = sum(p.entry_price * p.size for p in self.open_positions.values())
        return max(self.balance - invested, 0.0)

    @property
    def total_invested(self) -> float:
        return sum(p.entry_price * p.size for p in self.open_positions.values())

    def update_prices(self, prices: Dict[str, float]):
        self._prices.update(prices)
        for pos in self.open_positions.values():
            if pos.token_id in prices:
                pos.current_price = prices[pos.token_id]
                pos.update_pnl()

    async def execute_signal(self, signal) -> Optional[Order]:
        """Execute a TradeSignal from strategies.py."""
        from strategies import Side as StrategySide
        exec_side = Side.YES if signal.yes_price_cents <= 50 else Side.NO
        contracts = max(1, int(signal.size / (signal.yes_price_cents / 100.0)))
        return await self._place_order(signal.market, exec_side, contracts, signal.yes_price_cents)

    async def _place_order(
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
                f"@ {yes_price_cents}c | {market.question}"
            )
            order.status = OrderStatus.FILLED
            order.filled_count = contracts
            self._record_position(market, side, contracts, yes_price_cents / 100.0)
            self.order_history.append(order)
            return order

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
            ko = resp.get("order", {})
            order.status = OrderStatus.FILLED if "filled" in ko.get("status", "") else OrderStatus.PENDING
            order.filled_count = ko.get("filled_count", 0)
            if order.filled_count > 0:
                self._record_position(market, side, order.filled_count, yes_price_cents / 100.0)
            self.order_history.append(order)
            return order
        except Exception as e:
            logger.error(f"Order failed for {market.slug}: {e}")
            order.status = OrderStatus.FAILED
            self.order_history.append(order)
            return order

    def _record_position(self, market: Market, side: Side, contracts: int, price: float):
        token_id = market.slug + ("-YES" if side == Side.YES else "-NO")
        pos = Position(
            market_slug=market.slug,
            question=market.question,
            token_id=token_id,
            side=side,
            size=float(contracts),
            entry_price=price,
            current_price=price,
            strike_temp=market.strike,
            series_ticker=market.series_ticker,
        )
        self.open_positions[market.slug] = pos
        self.balance -= price * contracts
        logger.info(f"Position opened: {market.slug} {side.value} x{contracts} @ {price:.2f}  balance=${self.balance:.2f}")

    async def evaluate_positions_with_data(
        self,
        prices: dict,
        _unused1: str,
        _unused2: float,
        forecasts: dict,
    ) -> List[Position]:
        """Check open positions for exit conditions. Returns list of closed positions."""
        closed = []
        for slug, pos in list(self.open_positions.items()):
            cur = prices.get(pos.token_id, pos.entry_price)
            pos.update_pnl(cur)

            should_close = False
            reason = ""

            # Take profit at 40%+ gain
            if pos.pnl / max(pos.entry_price * pos.size, 0.01) >= 0.40:
                should_close = True
                reason = "take_profit"
            # Stop loss at 25%+ loss
            elif pos.pnl / max(pos.entry_price * pos.size, 0.01) <= -0.25:
                should_close = True
                reason = "stop_loss"
            # Time exit after 24h
            elif pos.age_seconds > 86400:
                should_close = True
                reason = "time_exit"

            if should_close:
                pos.exit_reason = reason
                self.balance += cur * pos.size
                self.daily_pnl += pos.pnl
                self.total_pnl += pos.pnl
                self.trade_history.append(pos)
                del self.open_positions[slug]
                closed.append(pos)
                logger.info(f"Position closed: {slug} reason={reason} pnl=${pos.pnl:+.2f}")

        return closed

    async def close(self):
        pass
