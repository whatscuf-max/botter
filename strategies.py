"""
Strategy Engine

Crypto confidence uses 6 independent indicators:
  1. RSI (14-period) - overbought/oversold
  2. Short MA cross (3 vs 8 period)
  3. Medium MA cross (8 vs 21 period)
  4. Short-term momentum (3-bar % change)
  5. Medium-term momentum (8-bar % change)
  6. Volatility regime (high vol = lower confidence)

Confidence = weighted agreement of indicators, capped 0.55-0.82
Direction locked for 3 minutes to prevent contradictions.
Must have 2+ agreeing indicators to trade.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from market_data import Market

logger = logging.getLogger("polymarket_bot.strategy")


class SignalType(Enum):
    ARBITRAGE = "arbitrage"
    MOMENTUM = "momentum"
    WEATHER = "weather"
    DIRECTIONAL = "directional"
    MEAN_REVERT = "mean_revert"
    MARKET_MAKE = "market_make"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class TradeSignal:
    signal_type: SignalType
    market: Market
    side: Side
    token_id: str
    outcome: str
    price: float
    size: float
    confidence: float
    reasoning: str
    timestamp: float = field(default_factory=time.time)
    paired_signal: Optional["TradeSignal"] = None


# ─── Market Identification ────────────────────────────────────

def is_crypto_up_or_down(market: Market) -> bool:
    q = market.question.lower()
    return "up or down" in q and any(coin in q for coin in [
        "bitcoin", "ethereum", "solana", "xrp", "dogecoin",
        "cardano", "bnb", "litecoin", "chainlink", "polkadot",
        "polygon", "sui", "pepe", "bonk", "near", "aptos",
        "arbitrum", "optimism",
    ])


def is_crypto_price_target(market: Market) -> bool:
    q = market.question.lower()
    return any(w in q for w in ["above $", "below $", "hit $", "reach $"]) and \
           any(coin in q for coin in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol"])


def is_weather_market(market: Market) -> bool:
    return "temperature" in market.question.lower()


def categorize_market(market: Market) -> Optional[str]:
    if is_crypto_up_or_down(market) or is_crypto_price_target(market):
        return "crypto"
    if is_weather_market(market):
        return "weather"
    return None


# ─── Technical Indicators ────────────────────────────────────

def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calc_sma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calc_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * mult + ema
    return ema


def calc_volatility(prices: List[float], period: int = 20) -> Optional[float]:
    """Calculate annualized volatility as a percentage."""
    if len(prices) < period + 1:
        return None
    returns = [(prices[i] / prices[i - 1]) - 1 for i in range(1, len(prices))]
    recent = returns[-period:]
    if not recent:
        return None
    mean_r = sum(recent) / len(recent)
    variance = sum((r - mean_r) ** 2 for r in recent) / len(recent)
    return math.sqrt(variance) * 100  # As percentage


def calc_momentum(prices: List[float], lookback: int) -> Optional[float]:
    """Percentage change over lookback periods."""
    if len(prices) < lookback + 1:
        return None
    old = prices[-(lookback + 1)]
    if old == 0:
        return None
    return (prices[-1] - old) / old


# ─── Strategy: Arbitrage ──────────────────────────────────────

class ArbitrageStrategy:
    def __init__(self, min_spread=0.015, fee_rate=0.02, min_liquidity=10.0):
        self.min_spread = min_spread
        self.fee_rate = fee_rate
        self.min_liquidity = min_liquidity

    def scan(self, markets: List[Market], balance: float) -> List[TradeSignal]:
        signals = []
        for market in markets:
            if not market.yes_price or not market.no_price:
                continue
            ya = self._best_ask(market, "yes")
            na = self._best_ask(market, "no")
            if ya is None or na is None:
                continue
            combined = ya + na
            spread = 1.0 - combined
            net = spread - self.fee_rate
            if net <= 0 or spread < self.min_spread:
                continue
            yl = self._ask_liq(market, "yes")
            nl = self._ask_liq(market, "no")
            if yl < self.min_liquidity or nl < self.min_liquidity:
                continue
            size = min(balance * 0.10, yl * 0.5, nl * 0.5, 100.0)
            if size < 1.0:
                continue
            ys = TradeSignal(SignalType.ARBITRAGE, market, Side.BUY,
                market.yes_token_id or "", "Yes", ya, size,
                min(net / 0.03, 0.90),
                f"ARB: {ya:.3f}+{na:.3f}={combined:.3f} net={net:.3f}")
            ns = TradeSignal(SignalType.ARBITRAGE, market, Side.BUY,
                market.no_token_id or "", "No", na, size,
                min(net / 0.03, 0.90), "ARB pair")
            ys.paired_signal = ns
            ns.paired_signal = ys
            signals.append(ys)
        return sorted(signals, key=lambda s: s.confidence, reverse=True)

    def _best_ask(self, m, side):
        for o in m.outcomes:
            if o.outcome.lower() == side:
                if o.order_book and o.order_book.best_ask is not None:
                    return o.order_book.best_ask
                return o.price
        return None

    def _ask_liq(self, m, side):
        for o in m.outcomes:
            if o.outcome.lower() == side:
                if o.order_book:
                    return o.order_book.ask_liquidity
        return 0.0


# ─── Strategy: Crypto Momentum (deep confidence) ─────────────

class CryptoMomentumStrategy:
    """
    6-indicator confidence system:
      Each indicator votes UP or DOWN with a weight.
      Confidence = weighted agreement ratio, scaled 0.55-0.82.
      Direction locked 3 min to prevent contradictions.
      Minimum 2 agreeing indicators required.
    """

    def __init__(self, min_confidence=0.55):
        self.min_confidence = min_confidence
        self._locked_direction = None
        self._lock_time = 0
        self._lock_duration = 180  # 3 minutes

    def analyze(self, crypto_markets: List[Market], btc_prices: List[float],
                balance: float, max_position_pct: float = 0.08) -> List[TradeSignal]:
        signals = []
        if not crypto_markets or len(btc_prices) < 2:
            return signals

        direction, confidence, reasons = self._deep_analysis(btc_prices)

        if confidence < self.min_confidence:
            logger.info(f"Crypto: {direction} conf={confidence:.2f} below {self.min_confidence}, skip")
            return signals

        # Direction lock
        now = time.time()
        if self._locked_direction and self._locked_direction != direction:
            if now - self._lock_time < self._lock_duration:
                secs_left = int(self._lock_duration - (now - self._lock_time))
                logger.info(f"Crypto: direction flip blocked ({secs_left}s lock remaining)")
                return signals

        self._locked_direction = direction
        self._lock_time = now

        # Trade top 2 markets
        trades = 0
        for market in crypto_markets:
            if trades >= 2:
                break
            if not market.yes_token_id or not market.no_token_id:
                continue

            q = market.question.lower()
            outcome, token_id, price = self._pick_side(market, q, direction)
            if not token_id or price is None:
                continue
            if price > 0.92 or price < 0.05:
                continue

            size = min(balance * max_position_pct * confidence, balance * 0.07)
            if size < 0.50:
                continue

            signals.append(TradeSignal(
                SignalType.MOMENTUM, market, Side.BUY,
                token_id, outcome, price, size, confidence,
                f"CRYPTO {direction.upper()}: {' | '.join(reasons)}",
            ))
            trades += 1
            logger.info(
                f"CRYPTO: {direction.upper()} | {market.question[:55]} | "
                f"{outcome}@{price:.3f} conf={confidence:.2f} ${size:.2f}"
            )

        return signals

    def _deep_analysis(self, prices: List[float]) -> Tuple[str, float, List[str]]:
        """6-indicator analysis returning (direction, confidence, reasons)."""
        votes = []  # (direction, weight, reason)

        # ── 1. RSI (14) ──
        rsi = calc_rsi(prices, 14)
        if rsi is not None:
            if rsi < 25:
                votes.append(("up", 1.2, f"RSI={rsi:.0f} deeply oversold"))
            elif rsi < 35:
                votes.append(("up", 0.8, f"RSI={rsi:.0f} oversold"))
            elif rsi < 45:
                votes.append(("up", 0.3, f"RSI={rsi:.0f} mild oversold"))
            elif rsi > 75:
                votes.append(("down", 1.2, f"RSI={rsi:.0f} deeply overbought"))
            elif rsi > 65:
                votes.append(("down", 0.8, f"RSI={rsi:.0f} overbought"))
            elif rsi > 55:
                votes.append(("down", 0.3, f"RSI={rsi:.0f} mild overbought"))
            # 45-55 = dead zone, no vote

        # ── 2. Short MA cross (3 vs 8) ──
        sma3 = calc_sma(prices, 3)
        sma8 = calc_sma(prices, 8)
        if sma3 is not None and sma8 is not None and sma8 != 0:
            spread = (sma3 - sma8) / sma8
            if spread > 0.002:
                votes.append(("up", 0.7, f"SMA3>SMA8 +{spread:.3%}"))
            elif spread < -0.002:
                votes.append(("down", 0.7, f"SMA3<SMA8 {spread:.3%}"))
            elif spread > 0.0005:
                votes.append(("up", 0.3, f"SMA3~>SMA8 +{spread:.3%}"))
            elif spread < -0.0005:
                votes.append(("down", 0.3, f"SMA3~<SMA8 {spread:.3%}"))

        # ── 3. Medium MA cross (8 vs 21) ──
        ema8 = calc_ema(prices, 8)
        ema21 = calc_ema(prices, 21)
        if ema8 is not None and ema21 is not None and ema21 != 0:
            spread = (ema8 - ema21) / ema21
            if spread > 0.003:
                votes.append(("up", 0.9, f"EMA8>EMA21 +{spread:.3%}"))
            elif spread < -0.003:
                votes.append(("down", 0.9, f"EMA8<EMA21 {spread:.3%}"))

        # ── 4. Short momentum (3-bar) ──
        mom3 = calc_momentum(prices, 3)
        if mom3 is not None:
            if mom3 > 0.003:
                votes.append(("up", 0.8, f"3-bar +{mom3:.2%}"))
            elif mom3 < -0.003:
                votes.append(("down", 0.8, f"3-bar {mom3:.2%}"))
            elif mom3 > 0.001:
                votes.append(("up", 0.3, f"3-bar mild +{mom3:.2%}"))
            elif mom3 < -0.001:
                votes.append(("down", 0.3, f"3-bar mild {mom3:.2%}"))

        # ── 5. Medium momentum (8-bar) ──
        mom8 = calc_momentum(prices, 8)
        if mom8 is not None:
            if mom8 > 0.005:
                votes.append(("up", 0.7, f"8-bar +{mom8:.2%}"))
            elif mom8 < -0.005:
                votes.append(("down", 0.7, f"8-bar {mom8:.2%}"))

        # ── 6. Volatility adjustment ──
        vol = calc_volatility(prices, 20)
        vol_penalty = 0.0
        if vol is not None:
            if vol > 3.0:
                vol_penalty = 0.10  # High vol = reduce confidence
                votes.append(("neutral", 0.0, f"HighVol={vol:.1f}% (-10% conf)"))
            elif vol > 2.0:
                vol_penalty = 0.05
                votes.append(("neutral", 0.0, f"ModVol={vol:.1f}% (-5% conf)"))

        if not votes:
            return "neutral", 0.0, ["No data"]

        # Count votes
        up_votes = [(w, r) for d, w, r in votes if d == "up"]
        dn_votes = [(w, r) for d, w, r in votes if d == "down"]
        up_weight = sum(w for w, _ in up_votes)
        dn_weight = sum(w for w, _ in dn_votes)
        total = up_weight + dn_weight
        up_count = len(up_votes)
        dn_count = len(dn_votes)

        reasons = [r for _, _, r in votes if r]

        # Need at least 2 agreeing indicators
        if max(up_count, dn_count) < 2:
            return ("up" if up_weight > dn_weight else "down"), 0.45, reasons

        # Direction
        if up_weight > dn_weight:
            direction = "up"
            agreement = up_weight / total if total > 0 else 0
            count = up_count
        else:
            direction = "down"
            agreement = dn_weight / total if total > 0 else 0
            count = dn_count

        # Confidence calculation
        # Base: map agreement (0.5-1.0) to confidence (0.55-0.75)
        base = 0.55 + (agreement - 0.5) * 0.40  # 0.55 to 0.75

        # Bonus for more agreeing indicators
        indicator_bonus = min((count - 2) * 0.03, 0.09)  # +3% per extra, max +9%

        # Apply volatility penalty
        confidence = base + indicator_bonus - vol_penalty

        # Clamp to 0.55 - 0.82
        confidence = max(0.55, min(confidence, 0.82))

        logger.info(
            f"CRYPTO ANALYSIS: {direction.upper()} conf={confidence:.2f} | "
            f"UP:{up_count}({up_weight:.1f}) DN:{dn_count}({dn_weight:.1f}) | "
            f"agree={agreement:.0%} bonus={indicator_bonus:.2f} vol_pen={vol_penalty:.2f} | "
            f"{', '.join(reasons)}"
        )

        return direction, confidence, reasons

    def _pick_side(self, market, q, direction):
        """Pick which token to buy based on market question and predicted direction."""
        if direction == "up":
            if "up or down" in q:
                return "Yes", market.yes_token_id, market.yes_price
            elif any(w in q for w in ["above $", "reach $", "hit $"]):
                return "Yes", market.yes_token_id, market.yes_price
            elif "below $" in q:
                return "No", market.no_token_id, market.no_price
            else:
                return "Yes", market.yes_token_id, market.yes_price
        else:
            if "up or down" in q:
                return "No", market.no_token_id, market.no_price
            elif any(w in q for w in ["above $", "reach $", "hit $"]):
                return "No", market.no_token_id, market.no_price
            elif "below $" in q:
                return "Yes", market.yes_token_id, market.yes_price
            else:
                return "No", market.no_token_id, market.no_price


# ─── Master Orchestrator ──────────────────────────────────────

class StrategyEngine:
    def __init__(self, config):
        self.config = config
        tc = config.trading
        self.arbitrage = ArbitrageStrategy(
            min_spread=tc.min_arb_spread, fee_rate=tc.arb_fee_rate,
            min_liquidity=tc.arb_min_liquidity)
        self.crypto = CryptoMomentumStrategy(min_confidence=tc.momentum_threshold)

    async def generate_signals(self, all_markets, crypto_markets, btc_prices,
                                price_histories, balance) -> List[TradeSignal]:
        signals = []
        arb = self.arbitrage.scan(all_markets, balance)
        signals.extend(arb)
        mom = self.crypto.analyze(
            crypto_markets, btc_prices, balance,
            self.config.trading.max_position_pct)
        signals.extend(mom)

        seen = set()
        unique = []
        for s in signals:
            if s.market.condition_id not in seen:
                seen.add(s.market.condition_id)
                unique.append(s)

        logger.info(f"Generated {len(unique)} signals: {len(arb)} arb, {len(mom)} crypto")
        return unique
