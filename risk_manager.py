"""
risk_manager.py  -  Risk controls for Kalshi Weather Trading Bot
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger("kalshi_bot.risk")


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    daily_trade_count: int = 0
    consecutive_losses: int = 0
    peak_balance: float = 0.0
    last_reset: float = field(default_factory=time.time)


class RiskManager:
    def __init__(self, config):
        self.config = config
        self.tc = config.trading
        self.state = RiskState(peak_balance=self.tc.starting_balance)
        self._reset_time = time.time()

    def should_pause(self, balance: float, starting_balance: float) -> bool:
        self._maybe_reset_daily()
        if self.state.peak_balance > 0:
            drawdown = (self.state.peak_balance - balance) / self.state.peak_balance
            if drawdown >= self.tc.max_drawdown:
                logger.warning(f"PAUSING: Drawdown {drawdown:.1%} >= limit {self.tc.max_drawdown:.1%}")
                return True
        if self.state.daily_pnl <= -(self.tc.daily_loss_limit * starting_balance):
            logger.warning(f"PAUSING: Daily loss ${self.state.daily_pnl:.2f} hit limit")
            return True
        if self.state.consecutive_losses >= self.tc.max_consecutive_losses:
            logger.warning(f"PAUSING: {self.state.consecutive_losses} consecutive losses")
            return True
        if balance > self.state.peak_balance:
            self.state.peak_balance = balance
        return False

    def filter_signals(
        self,
        signals: list,
        balance: float,
        available_balance: float,
        open_position_count: int,
        total_invested: float,
        trade_history: list,
    ) -> list:
        self._maybe_reset_daily()
        out = []
        for s in signals:
            if open_position_count >= self.tc.max_concurrent_positions:
                break
            if s.confidence < self.tc.min_confidence:
                continue
            if s.edge < self.tc.min_edge:
                continue
            if s.size > available_balance * 0.95:
                continue
            max_invest = balance * self.tc.max_invested_pct
            if total_invested + s.size > max_invest:
                continue
            if self.state.daily_trade_count >= self.tc.max_daily_trades:
                break
            out.append(s)
        logger.debug(f"Risk filter: {len(signals)} -> {len(out)} signals")
        return out

    def calculate_compound_size(self, base_size: float, balance: float, starting_balance: float) -> float:
        if starting_balance <= 0:
            return base_size
        scale = min(balance / starting_balance, 3.0)
        return round(min(base_size * scale, balance * self.tc.max_position_pct), 2)

    def record_trade_result(self, pnl: float):
        self._maybe_reset_daily()
        self.state.daily_pnl += pnl
        self.state.daily_trade_count += 1
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def _maybe_reset_daily(self):
        now = time.time()
        if now - self._reset_time >= 86400:
            logger.info(f"Daily reset | PnL={self.state.daily_pnl:+.2f} | Trades={self.state.daily_trade_count}")
            self.state.daily_pnl = 0.0
            self.state.daily_trade_count = 0
            self.state.consecutive_losses = 0
            self._reset_time = now
