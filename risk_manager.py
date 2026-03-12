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

        # Max drawdown from peak (hardcoded 25% since not in TradingConfig)
        max_drawdown = 0.25
        if self.state.peak_balance > 0:
            drawdown = (self.state.peak_balance - balance) / self.state.peak_balance
            if drawdown >= max_drawdown:
                logger.warning(f"PAUSING: Drawdown {drawdown:.1%} >= limit {max_drawdown:.1%}")
                return True

        # Daily loss limit uses max_daily_loss_pct from TradingConfig
        daily_loss_limit = self.tc.max_daily_loss_pct * starting_balance
        if self.state.daily_pnl <= -abs(daily_loss_limit):
            logger.warning(f"PAUSING: Daily loss ${self.state.daily_pnl:.2f} hit limit")
            return True

        # Consecutive losses (hardcoded 5)
        if self.state.consecutive_losses >= 5:
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
            min_conf = getattr(self.tc, "min_confidence", 0.55)
            if s.confidence < min_conf:
                continue
            out.append(s)
        logger.debug(f"Risk filter: {len(signals)} -> {len(out)} signals")
        return out

    def calculate_compound_size(
        self, base_size: float, balance: float, starting_balance: float
    ) -> float:
        if starting_balance <= 0:
            return base_size
        scale = min(balance / starting_balance, 3.0)
        new_size = base_size * scale
        max_pos = balance * self.tc.max_position_pct
        return round(min(new_size, max_pos), 2)

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
            logger.info(
                f"Daily reset | PnL={self.state.daily_pnl:+.2f} "
                f"| Trades={self.state.daily_trade_count}"
            )
            self.state.daily_pnl = 0.0
            self.state.daily_trade_count = 0
            self.state.consecutive_losses = 0
            self._reset_time = now
