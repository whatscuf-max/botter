"""
risk_manager.py  -  Risk controls for Kalshi Weather Trading Bot
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("kalshi_bot.risk")


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    daily_trade_count: int = 0
    consecutive_losses: int = 0
    peak_balance: float = 0.0
    last_reset: float = field(default_factory=time.time)


class RiskManager:
    """
    Enforces position limits, drawdown limits, daily loss limits,
    and signal quality filters for the Kalshi bot.
    """

    def __init__(self, config):
        self.config = config
        self.tc = config.trading
        self.state = RiskState(peak_balance=self.tc.starting_balance)
        self._reset_time = time.time()

    # ------------------------------------------------------------------
    # Core checks
    # ------------------------------------------------------------------

    def should_pause(self, balance: float, starting_balance: float) -> bool:
        """Return True if the bot should pause trading due to risk limits."""
        self._maybe_reset_daily()

        # Max drawdown from peak
        if self.state.peak_balance > 0:
            drawdown = (self.state.peak_balance - balance) / self.state.peak_balance
            if drawdown >= self.tc.max_drawdown:
                logger.warning(
                    f"PAUSING: Drawdown {drawdown:.1%} >= limit {self.tc.max_drawdown:.1%}"
                )
                return True

        # Daily loss limit
        if self.state.daily_pnl <= -abs(self.tc.daily_loss_limit * starting_balance):
            logger.warning(
                f"PAUSING: Daily loss ${self.state.daily_pnl:.2f} hit limit"
            )
            return True

        # Consecutive losses
        if self.state.consecutive_losses >= getattr(self.tc, "max_consecutive_losses", 5):
            logger.warning(
                f"PAUSING: {self.state.consecutive_losses} consecutive losses"
            )
            return True

        # Update peak
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
        """Filter signals that pass risk checks."""
        self._maybe_reset_daily()
        out = []

        for s in signals:
            # Max concurrent positions
            if open_position_count >= self.tc.max_concurrent_positions:
                logger.debug(f"SKIP (max positions): {s.market.question[:40]}")
                continue

            # Minimum confidence
            min_conf = getattr(self.tc, "min_confidence", 0.55)
            if s.confidence < min_conf:
                logger.debug(
                    f"SKIP (low confidence {s.confidence:.2f}): {s.market.question[:40]}"
                )
                continue

            # Minimum edge
            min_edge = getattr(self.tc, "min_edge", 0.03)
            if s.edge < min_edge:
                logger.debug(
                    f"SKIP (low edge {s.edge:.3f}): {s.market.question[:40]}"
                )
                continue

            # Available balance check
            if s.size > available_balance * 0.95:
                logger.debug(
                    f"SKIP (insufficient balance): {s.market.question[:40]}"
                )
                continue

            # Max portfolio concentration
            max_invest = balance * getattr(self.tc, "max_invested_pct", 0.80)
            if total_invested + s.size > max_invest:
                logger.debug(
                    f"SKIP (portfolio limit): {s.market.question[:40]}"
                )
                continue

            # Daily trade count
            max_daily = getattr(self.tc, "max_daily_trades", 50)
            if self.state.daily_trade_count >= max_daily:
                logger.warning(f"Daily trade limit {max_daily} reached")
                break

            out.append(s)

        logger.debug(f"Risk filter: {len(signals)} -> {len(out)} signals")
        return out

    def calculate_compound_size(
        self, base_size: float, balance: float, starting_balance: float
    ) -> float:
        """Scale position size proportionally with account growth."""
        if starting_balance <= 0:
            return base_size
        scale = balance / starting_balance
        # Cap growth multiplier at 3x to avoid runaway sizing
        scale = min(scale, 3.0)
        new_size = base_size * scale

        # Hard cap: never more than max_position_pct of balance
        max_pos = balance * self.tc.max_position_pct
        return round(min(new_size, max_pos), 2)

    def record_trade_result(self, pnl: float):
        """Call after each trade closes to update state."""
        self._maybe_reset_daily()
        self.state.daily_pnl += pnl
        self.state.daily_trade_count += 1
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _maybe_reset_daily(self):
        """Reset daily counters if a new calendar day has started."""
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
