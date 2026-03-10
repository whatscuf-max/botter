"""
Risk Manager
Enforces position limits, daily loss caps, and portfolio-level risk controls.
Runs as a guardian layer between the strategy engine and executor.
"""

import logging
import time
from typing import Dict, List

from strategies import TradeSignal, SignalType

logger = logging.getLogger("polymarket_bot.risk")


class RiskManager:
    """
    Risk management rules:
    1. Max position size: 5% of balance per trade (10% for arb)
    2. Max concurrent positions: 5
    3. Daily loss limit: 10% of starting balance
    4. Max exposure: 50% of balance invested at any time
    5. No duplicate markets
    6. Minimum confidence threshold
    7. Compounding: grow position sizes as balance grows
    """

    def __init__(self, config):
        self.config = config
        self._daily_pnl = 0.0
        self._daily_reset_time = time.time()
        self._traded_markets = set()  # condition_ids traded today

    def filter_signals(
        self,
        signals: List[TradeSignal],
        balance: float,
        available_balance: float,
        open_position_count: int,
        total_invested: float,
        trade_history: List[dict],
    ) -> List[TradeSignal]:
        """
        Filter signals through risk rules.
        Returns only approved signals.
        """
        # Reset daily tracking at midnight
        if time.time() - self._daily_reset_time > 86400:
            self._daily_pnl = 0.0
            self._daily_reset_time = time.time()
            self._traded_markets.clear()

        # Calculate daily PnL
        daily_trades = [
            t for t in trade_history
            if t.get("timestamp", 0) > self._daily_reset_time
        ]
        self._daily_pnl = sum(t.get("pnl", 0) for t in daily_trades)

        approved = []
        starting = self.config.trading.starting_balance

        for signal in signals:
            # Rule 1: Daily loss limit
            max_daily_loss = starting * self.config.trading.max_daily_loss_pct
            if self._daily_pnl < -max_daily_loss:
                logger.warning(
                    f"⚠️ DAILY LOSS LIMIT: ${self._daily_pnl:.2f} exceeds "
                    f"-${max_daily_loss:.2f}. Pausing trading."
                )
                break  # Stop all trading for the day

            # Rule 2: Max concurrent positions
            max_positions = self.config.trading.max_concurrent_positions
            if open_position_count + len(approved) >= max_positions:
                logger.debug(f"Max positions ({max_positions}) reached")
                continue

            # Rule 3: Max total exposure (50% of balance)
            max_exposure = balance * 0.50
            if total_invested + signal.size > max_exposure:
                logger.debug(f"Max exposure reached: ${total_invested:.2f}/{max_exposure:.2f}")
                continue

            # Rule 4: Position size limits
            if signal.signal_type == SignalType.ARBITRAGE:
                max_size = balance * 0.10  # Arb gets 10% max
            else:
                max_size = balance * self.config.trading.max_position_pct

            if signal.size > max_size:
                signal.size = max_size  # Cap it rather than reject

            # Rule 5: Minimum size after capping
            if signal.size < 1.0:
                continue

            # Rule 6: Must fit in available balance
            if signal.size > available_balance:
                signal.size = available_balance * 0.95  # Leave 5% buffer
                if signal.size < 1.0:
                    continue

            # Rule 7: No duplicate markets (within same day)
            if signal.market.condition_id in self._traded_markets:
                # Allow arb re-entry but not directional
                if signal.signal_type != SignalType.ARBITRAGE:
                    logger.debug(f"Already traded market: {signal.market.question[:40]}")
                    continue

            # Rule 8: Minimum confidence
            min_conf = 0.55 if signal.signal_type == SignalType.MOMENTUM else 0.50
            if signal.confidence < min_conf:
                logger.debug(
                    f"Low confidence {signal.confidence:.2f} < {min_conf} "
                    f"for {signal.signal_type.value}"
                )
                continue

            # Passed all checks
            approved.append(signal)
            self._traded_markets.add(signal.market.condition_id)

        if approved:
            logger.info(
                f"Risk approved {len(approved)}/{len(signals)} signals | "
                f"Daily PnL: ${self._daily_pnl:.2f} | "
                f"Open: {open_position_count} | "
                f"Invested: ${total_invested:.2f}"
            )

        return approved

    def calculate_compound_size(
        self, base_size: float, balance: float, starting_balance: float
    ) -> float:
        """
        Adjust position size based on compounding.
        As balance grows, position sizes grow proportionally.
        """
        if not self.config.trading.compound_profits:
            return base_size

        growth_factor = balance / starting_balance
        # Cap compound multiplier at 3x to prevent over-leverage
        multiplier = min(growth_factor, 3.0)
        return base_size * multiplier

    def should_pause(self, balance: float, starting_balance: float) -> bool:
        """Check if bot should pause trading."""
        # Pause if balance drops below 50% of starting
        if balance < starting_balance * 0.50:
            logger.critical(
                f"🚨 BALANCE CRITICAL: ${balance:.2f} "
                f"({balance/starting_balance:.0%} of starting)"
            )
            return True

        # Pause if daily loss limit hit
        max_loss = starting_balance * self.config.trading.max_daily_loss_pct
        if self._daily_pnl < -max_loss:
            return True

        return False
