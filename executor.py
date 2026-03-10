"""
Trade Executor - sell commands, unlimited positions, data-driven exits
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from strategies import Side, TradeSignal, SignalType

logger = logging.getLogger("polymarket_bot.executor")


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    order_id: str
    signal: TradeSignal
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0.0
    fill_size: float = 0.0
    fee: float = 0.0
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    error: str = ""


@dataclass
class Position:
    id: int
    market_condition_id: str
    question: str
    token_id: str
    outcome: str
    entry_price: float
    size: float
    cost: float
    signal_type: str
    reasoning: str
    confidence: float
    forecast_temp: float = 0.0
    forecast_unit: str = ""
    temp_range_low: float = 0.0
    temp_range_high: float = 0.0
    city: str = ""
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0

    @property
    def is_open(self):
        return self.closed_at is None

    @property
    def age_str(self):
        mins = (time.time() - self.opened_at) / 60
        if mins < 60:
            return f"{mins:.0f}m"
        hrs = mins / 60
        if hrs < 24:
            return f"{hrs:.1f}h"
        return f"{hrs / 24:.1f}d"

    def unrealized_pnl(self, cp):
        return (cp - self.entry_price) * self.size

    def unrealized_pnl_pct(self, cp):
        return (cp - self.entry_price) / self.entry_price if self.entry_price else 0


class TradeExecutor:
    def __init__(self, config, clob_client=None):
        self.config = config
        self.dry_run = config.dry_run
        self.clob_client = clob_client
        self.orders = {}
        self.positions: Dict[str, Position] = {}
        self.trade_history: List[dict] = []
        self._paper_balance = config.trading.starting_balance
        self._starting_balance = config.trading.starting_balance
        self._next_id = 1
        self._current_prices: Dict[str, float] = {}

    @property
    def balance(self):
        return self._paper_balance

    @property
    def open_positions(self):
        return sorted(
            [p for p in self.positions.values() if p.is_open], key=lambda p: p.id
        )

    @property
    def total_invested(self):
        return sum(p.cost for p in self.open_positions)

    @property
    def total_pnl(self):
        return sum(t.get("pnl", 0) for t in self.trade_history if t.get("type") == "close")

    @property
    def available_balance(self):
        return self.balance - self.total_invested

    def update_prices(self, prices):
        self._current_prices.update(prices)

    def sell_position(self, pos_id: int) -> str:
        for tid, pos in self.positions.items():
            if pos.id == pos_id and pos.is_open:
                cp = self._current_prices.get(tid, pos.entry_price)
                pos.exit_price = cp
                pos.pnl = (cp - pos.entry_price) * pos.size
                pos.closed_at = time.time()
                pos.exit_reason = "Manual sell"
                self._paper_balance += cp * pos.size
                self._record_close(pos)
                pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
                return f"SOLD #{pos.id}: {pos.question[:60]} | PnL: {pnl_s}"
        return f"Position #{pos_id} not found."

    def sell_all(self) -> str:
        if not self.open_positions:
            return "No open positions."
        lines = []
        for pos in list(self.open_positions):
            cp = self._current_prices.get(pos.token_id, pos.entry_price)
            pos.exit_price = cp
            pos.pnl = (cp - pos.entry_price) * pos.size
            pos.closed_at = time.time()
            pos.exit_reason = "Manual sell all"
            self._paper_balance += cp * pos.size
            self._record_close(pos)
            pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
            lines.append(f"  SOLD #{pos.id}: {pos.question[:50]} | {pnl_s}")
        return "\n".join(lines) + f"\n  Balance: ${self.balance:.2f}"

    async def execute_signal(self, signal: TradeSignal) -> Optional[Order]:
        if not self._validate(signal):
            return None
        if signal.signal_type == SignalType.ARBITRAGE and signal.paired_signal:
            yo = await self._exec(signal)
            if yo and yo.status == OrderStatus.FILLED:
                await self._exec(signal.paired_signal)
            return yo
        return await self._exec(signal)

    async def _exec(self, signal) -> Order:
        order = Order(order_id=str(uuid.uuid4())[:8], signal=signal)
        if not self.dry_run and self.clob_client:
            return await self._live(order)
        return await self._paper(order)

    async def _paper(self, order) -> Order:
        sig = order.signal
        shares = sig.size / sig.price if sig.price > 0 else 0
        fp = sig.price * 1.001
        cost = shares * fp
        if cost > self.available_balance:
            order.status = OrderStatus.FAILED
            return order
        order.status = OrderStatus.FILLED
        order.fill_price = fp
        order.fill_size = shares
        order.filled_at = time.time()
        self._paper_balance -= cost + cost * 0.001

        pid = self._next_id
        self._next_id += 1
        pos = Position(
            id=pid,
            market_condition_id=sig.market.condition_id,
            question=sig.market.question,
            token_id=sig.token_id,
            outcome=sig.outcome,
            entry_price=fp,
            size=shares,
            cost=cost,
            signal_type=sig.signal_type.value,
            reasoning=sig.reasoning,
            confidence=sig.confidence,
        )

        # FIX 5: Parse weather temp from reasoning — handles both FCST and OBS formats
        # Matches: "Forecast=46F", "Forecast=46.9F", "Forecast=46C", "Forecast=46.9C"
        tm = re.search(r'Forecast=([\d.]+)[FC]', sig.reasoning)
        if tm:
            pos.forecast_temp = float(tm.group(1))

        rm = re.search(r'Range=(\d+)-(\d+)', sig.reasoning)
        if rm:
            pos.temp_range_low = float(rm.group(1))
            pos.temp_range_high = float(rm.group(2))

        um = re.search(r'Range=\d+-\d+([FC])', sig.reasoning)
        if um:
            pos.forecast_unit = um.group(1)

        for c in [
            "new york", "london", "paris", "seoul", "ankara", "lucknow",
            "wellington", "munich", "sao paulo", "buenos aires", "toronto",
            "miami", "atlanta", "chicago", "seattle", "dallas",
        ]:
            if c in sig.market.question.lower():
                pos.city = c.title()
                break

        self.positions[sig.token_id] = pos
        self.trade_history.append({
            "order_id": order.order_id,
            "signal_type": sig.signal_type.value,
            "market": pos.question,
            "outcome": pos.outcome,
            "price": fp,
            "shares": shares,
            "cost": cost,
            "reasoning": pos.reasoning,
            "confidence": pos.confidence,
            "timestamp": time.time(),
            "balance_after": self.balance,
        })
        logger.info(f"FILL #{pid}: {sig.outcome} {shares:.0f}sh @ ${fp:.4f} = ${cost:.2f}")
        return order

    async def _live(self, order) -> Order:
        sig = order.signal
        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            shares = sig.size / sig.price if sig.price > 0 else 0
            resp = self.clob_client.create_and_post_order({
                "token_id": sig.token_id,
                "price": sig.price,
                "size": shares,
                "side": BUY if sig.side == Side.BUY else SELL,
            })
            if resp and resp.get("success"):
                order.status = OrderStatus.FILLED
                order.fill_price = sig.price
                order.fill_size = shares
                order.filled_at = time.time()
                self._paper_balance -= shares * sig.price
                pid = self._next_id
                self._next_id += 1
                pos = Position(
                    id=pid,
                    market_condition_id=sig.market.condition_id,
                    question=sig.market.question,
                    token_id=sig.token_id,
                    outcome=sig.outcome,
                    entry_price=sig.price,
                    size=shares,
                    cost=shares * sig.price,
                    signal_type=sig.signal_type.value,
                    reasoning=sig.reasoning,
                    confidence=sig.confidence,
                )
                self.positions[sig.token_id] = pos
            else:
                order.status = OrderStatus.FAILED
        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error = str(e)
        return order

    async def evaluate_positions_with_data(
        self, current_prices, crypto_direction, crypto_confidence, weather_forecasts
    ):
        closed = []
        for tid, pos in list(self.positions.items()):
            # FIX 5: pos.forecast_temp == 0 means temp was never parsed — skip non-weather
            if not pos.is_open or pos.forecast_temp == 0:
                continue
            city = None
            q = pos.question.lower()
            for c in weather_forecasts:
                if c.lower() in q:
                    city = c
                    break
            if not city or city not in weather_forecasts:
                continue
            fc = weather_forecasts[city]
            new_temp = fc.get("temp_f", 0) if pos.forecast_unit == "F" else fc.get("temp_c", 0)
            if new_temp == 0:
                continue
            shift = abs(new_temp - pos.forecast_temp)
            if shift < 3.0:
                continue
            range_mid = (pos.temp_range_low + pos.temp_range_high) / 2
            should_exit = False
            reason = ""
            if pos.outcome == "Yes":
                new_dist = abs(new_temp - range_mid)
                if new_dist > 4.0:
                    should_exit = True
                    reason = f"Forecast {pos.forecast_temp:.1f}->{new_temp:.1f} (now {new_dist:.1f} from range)"
            elif pos.outcome == "No":
                if pos.temp_range_low <= new_temp <= pos.temp_range_high:
                    should_exit = True
                    reason = f"Forecast {pos.forecast_temp:.1f}->{new_temp:.1f} (now IN range)"
            if should_exit:
                cp = current_prices.get(tid, pos.entry_price)
                pos.exit_price = cp
                pos.pnl = (cp - pos.entry_price) * pos.size
                pos.closed_at = time.time()
                pos.exit_reason = reason
                self._paper_balance += cp * pos.size
                closed.append(pos)
                self._record_close(pos)
        return closed

    def _validate(self, sig):
        if sig.size < 0.50 or sig.price <= 0 or sig.price >= 1.0:
            return False
        if len(self.open_positions) >= self.config.trading.max_concurrent_positions:
            return False
        if sig.size > self.available_balance:
            return False
        return True

    def _record_close(self, pos):
        self.trade_history.append({
            "type": "close",
            "market": pos.question,
            "outcome": pos.outcome,
            "exit_reason": pos.exit_reason,
            "pnl": pos.pnl,
            "entry": pos.entry_price,
            "exit": pos.exit_price,
            "timestamp": time.time(),
            "balance_after": self.balance,
        })
        pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
        logger.info(
            f"CLOSE #{pos.id}: {pos.question[:50]} | {pos.outcome} | "
            f"PnL={pnl_s} | reason={pos.exit_reason}"
        )
