# Kalshi Weather Trading Strategies
# Adapted from the original Polymarket strategies -- crypto/arb logic preserved,
# weather signal updated for Kalshi temperature market ticker format.

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from config import KALSHI_WEATHER_SERIES
from market_data import Market

logger = logging.getLogger("kalshi_bot.strategies")


class SignalType(Enum):
    ARBITRAGE = "arbitrage"
    MOMENTUM = "momentum"
    WEATHER = "weather"
    DIRECTIONAL = "directional"
    MEAN_REVERT = "mean_revert"


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class TradeSignal:
    market: Market
    signal_type: SignalType
    side: Side
    confidence: float           # 0.0 - 1.0
    yes_price_cents: int        # price to use for order (Kalshi cents)
    contracts: int = 1
    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_weather_market(market: Market) -> bool:
    # Kalshi weather markets have a series_ticker starting with KXHIGH
    if market.series_ticker and market.series_ticker in KALSHI_WEATHER_SERIES:
        return True
    # Fallback: check slug prefix
    return market.slug.upper().startswith("KXHIGH")


def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_sma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calc_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = prices[-period]
    for p in prices[-period + 1:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_momentum(prices: List[float], lookback: int = 3) -> Optional[float]:
    if len(prices) < lookback + 1:
        return None
    return prices[-1] - prices[-(lookback + 1)]


def calc_volatility(prices: List[float], period: int = 10) -> Optional[float]:
    if len(prices) < period:
        return None
    window = prices[-period:]
    mean = sum(window) / len(window)
    variance = sum((p - mean) ** 2 for p in window) / len(window)
    return variance ** 0.5


# ---------------------------------------------------------------------------
# Arbitrage Strategy
# Kalshi: YES price + NO price should sum to ~$1.00 (100 cents)
# If combined < $1.00 after fees, there is an arb opportunity.
# ---------------------------------------------------------------------------

class ArbitrageStrategy:
    def __init__(self, min_spread: float = 0.015, fee_rate: float = 0.02, min_liquidity: float = 10.0):
        self.min_spread = min_spread
        self.fee_rate = fee_rate
        self.min_liquidity = min_liquidity

    def analyze(self, market: Market) -> Optional[TradeSignal]:
        spread = market.arb_spread
        if spread is None:
            return None
        net_spread = spread - self.fee_rate
        if net_spread < self.min_spread:
            return None
        if market.liquidity < self.min_liquidity:
            return None

        # Buy both YES and NO -- we want the cheaper side first
        yes_p = market.yes_price or 0.5
        no_p = market.no_price or 0.5
        # Signal on the YES side (bot will separately handle NO leg)
        yes_cents = int(yes_p * 100)
        confidence = min(0.5 + net_spread * 10, 0.95)
        return TradeSignal(
            market=market,
            signal_type=SignalType.ARBITRAGE,
            side=Side.BUY,
            confidence=confidence,
            yes_price_cents=yes_cents,
            reason=f"Arb spread={net_spread:.3f} combined={market.combined_price:.3f}",
        )


# ---------------------------------------------------------------------------
# Weather Strategy
# Uses NWS forecast vs Kalshi strike to generate directional signals.
# ---------------------------------------------------------------------------

class WeatherStrategy:
    def __init__(self, forecast_cache: dict, min_confidence: float = 0.55):
        # forecast_cache: {series_ticker: {"forecast_high": float, "station": str}}
        self.forecast_cache = forecast_cache
        self.min_confidence = min_confidence

    def analyze(self, market: Market) -> Optional[TradeSignal]:
        if not is_weather_market(market):
            return None
        series = market.series_ticker
        if not series or series not in self.forecast_cache:
            return None

        forecast = self.forecast_cache[series]
        forecast_high = forecast.get("forecast_high")
        if forecast_high is None or market.strike == 0.0:
            return None

        diff = forecast_high - market.strike
        # Only trade when forecast is meaningfully above or below strike
        if abs(diff) < 2.0:
            return None

        yes_price = market.yes_price or 0.5
        no_price = market.no_price or 0.5

        if diff > 0:
            # Forecast high > strike -> YES is likely to win
            edge = yes_price - 0.5
            if yes_price > 0.85:
                return None  # Too expensive
            confidence = min(0.5 + abs(diff) * 0.04, 0.92)
            if confidence < self.min_confidence:
                return None
            return TradeSignal(
                market=market,
                signal_type=SignalType.WEATHER,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int(yes_price * 100),
                reason=f"Forecast {forecast_high:.1f}F > strike {market.strike:.1f}F (diff={diff:+.1f})",
            )
        else:
            # Forecast high < strike -> NO is likely to win (buy NO = low yes price)
            if no_price > 0.85:
                return None
            confidence = min(0.5 + abs(diff) * 0.04, 0.92)
            if confidence < self.min_confidence:
                return None
            return TradeSignal(
                market=market,
                signal_type=SignalType.WEATHER,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int(yes_price * 100),
                reason=f"Forecast {forecast_high:.1f}F < strike {market.strike:.1f}F (diff={diff:+.1f})",
            )


# ---------------------------------------------------------------------------
# Momentum Strategy (preserved from original -- works on price history)
# ---------------------------------------------------------------------------

class MomentumStrategy:
    def __init__(self, threshold: float = 0.55):
        self.threshold = threshold

    def analyze(self, market: Market, price_history: List[float]) -> Optional[TradeSignal]:
        if len(price_history) < 22:
            return None

        rsi = calc_rsi(price_history)
        sma3 = calc_sma(price_history, 3)
        sma8 = calc_sma(price_history, 8)
        ema8 = calc_ema(price_history, 8)
        ema21 = calc_ema(price_history, 21)
        mom3 = calc_momentum(price_history, 3)
        mom8 = calc_momentum(price_history, 8)
        vol = calc_volatility(price_history, 10)

        if any(v is None for v in [rsi, sma3, sma8, ema8, ema21, mom3, mom8, vol]):
            return None

        bullish = sum([
            rsi > 55,
            sma3 > sma8,
            ema8 > ema21,
            mom3 > 0.01,
            mom8 > 0.02,
            vol < 0.05,
        ])
        bearish = sum([
            rsi < 45,
            sma3 < sma8,
            ema8 < ema21,
            mom3 < -0.01,
            mom8 < -0.02,
        ])

        current_price = price_history[-1]

        if bullish >= 4 and current_price < 0.80:
            confidence = 0.5 + (bullish / 6) * 0.4
            return TradeSignal(
                market=market,
                signal_type=SignalType.MOMENTUM,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int(current_price * 100),
                reason=f"Bullish momentum {bullish}/6 indicators RSI={rsi:.1f}",
            )
        if bearish >= 3 and current_price > 0.20:
            confidence = 0.5 + (bearish / 5) * 0.35
            return TradeSignal(
                market=market,
                signal_type=SignalType.MOMENTUM,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int((1 - current_price) * 100),  # Buy NO
                reason=f"Bearish momentum {bearish}/5 indicators RSI={rsi:.1f}",
            )
        return None


# ---------------------------------------------------------------------------
# Strategy Engine -- orchestrates all strategies
# ---------------------------------------------------------------------------

class StrategyEngine:
    def __init__(self, config, forecast_cache: dict):
        self.arb = ArbitrageStrategy(
            min_spread=config.trading.min_arb_spread,
            fee_rate=config.trading.arb_fee_rate,
            min_liquidity=config.trading.arb_min_liquidity,
        )
        self.weather = WeatherStrategy(forecast_cache, min_confidence=config.trading.momentum_threshold)
        self.momentum = MomentumStrategy(threshold=config.trading.momentum_threshold)

    def analyze(self, market: Market, price_history: List[float] = None) -> List[TradeSignal]:
        signals = []

        arb_sig = self.arb.analyze(market)
        if arb_sig:
            signals.append(arb_sig)

        weather_sig = self.weather.analyze(market)
        if weather_sig:
            signals.append(weather_sig)

        if price_history:
            mom_sig = self.momentum.analyze(market, price_history)
            if mom_sig:
                signals.append(mom_sig)

        return signals
