# Kalshi Weather Trading Strategies

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from config import KALSHI_WEATHER_SERIES
from market_data import KalshiMarket as Market

logger = logging.getLogger("kalshi_bot.strategies")

class SignalType(Enum):
    ARBITRAGE = "arbitrage"
    MOMENTUM  = "momentum"
    WEATHER   = "weather"

class Side(Enum):
    BUY  = "buy"
    SELL = "sell"

@dataclass
class TradeSignal:
    market: Market
    signal_type: SignalType
    side: Side
    confidence: float
    yes_price_cents: int
    contracts: int = 1
    size: float = 1.0
    edge: float = 0.0
    reason: str = ""

    @property
    def outcome(self) -> str:
        return "YES" if self.yes_price_cents <= 50 else "NO"

    @property
    def price(self) -> float:
        return self.yes_price_cents / 100.0

def is_weather_market(market: Market) -> bool:
    if market.series_ticker and market.series_ticker in KALSHI_WEATHER_SERIES:
        return True
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
    return 100 - (100 / (1 + avg_gain / avg_loss))

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

class ArbitrageStrategy:
    def __init__(self, min_spread: float = 0.015, fee_rate: float = 0.02, min_liquidity: float = 10.0):
        self.min_spread   = min_spread
        self.fee_rate     = fee_rate
        self.min_liquidity = min_liquidity

    def analyze(self, market: Market) -> Optional[TradeSignal]:
        spread = market.arb_spread
        if spread is None:
            return None
        net_spread = spread - self.fee_rate
        if net_spread < self.min_spread or market.liquidity < self.min_liquidity:
            return None
        yes_p    = market.yes_price or 0.5
        yes_cents = int(yes_p * 100)
        confidence = min(0.5 + net_spread * 10, 0.95)
        edge = net_spread
        return TradeSignal(
            market=market,
            signal_type=SignalType.ARBITRAGE,
            side=Side.BUY,
            confidence=confidence,
            yes_price_cents=yes_cents,
            edge=edge,
            reason=f"Arb spread={net_spread:.3f} combined={market.combined_price:.3f}",
        )

class InternalWeatherStrategy:
    """Directional weather signal using a pre-populated forecast cache."""

    def __init__(self, forecast_cache: dict, min_confidence: float = 0.55):
        self.forecast_cache = forecast_cache
        self.min_confidence = min_confidence

    def analyze(self, market: Market) -> Optional[TradeSignal]:
        if not is_weather_market(market):
            return None
        series = market.series_ticker
        if not series or series not in self.forecast_cache:
            return None
        forecast_high = self.forecast_cache[series].get("forecast_high")
        if forecast_high is None or market.strike == 0.0:
            return None
        diff = forecast_high - market.strike
        if abs(diff) < 2.0:
            return None
        yes_price = market.yes_price or 0.5
        no_price  = market.no_price  or 0.5
        if diff > 0:
            if yes_price > 0.85:
                return None
            confidence = min(0.5 + abs(diff) * 0.04, 0.92)
            if confidence < self.min_confidence:
                return None
            edge = confidence - yes_price
            return TradeSignal(
                market=market,
                signal_type=SignalType.WEATHER,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int(yes_price * 100),
                edge=edge,
                reason=f"Forecast {forecast_high:.1f}F > strike {market.strike:.1f}F (diff={diff:+.1f})",
            )
        else:
            if no_price > 0.85:
                return None
            confidence = min(0.5 + abs(diff) * 0.04, 0.92)
            if confidence < self.min_confidence:
                return None
            edge = confidence - no_price
            return TradeSignal(
                market=market,
                signal_type=SignalType.WEATHER,
                side=Side.BUY,
                confidence=confidence,
                yes_price_cents=int(yes_price * 100),
                edge=edge,
                reason=f"Forecast {forecast_high:.1f}F < strike {market.strike:.1f}F (diff={diff:+.1f})",
            )

class MomentumStrategy:
    def __init__(self, threshold: float = 0.55):
        self.threshold = threshold

    def analyze(self, market: Market, price_history: List[float]) -> Optional[TradeSignal]:
        if len(price_history) < 22:
            return None
        rsi  = calc_rsi(price_history)
        sma3 = calc_sma(price_history, 3)
        sma8 = calc_sma(price_history, 8)
        ema8  = calc_ema(price_history, 8)
        ema21 = calc_ema(price_history, 21)
        mom3  = calc_momentum(price_history, 3)
        mom8  = calc_momentum(price_history, 8)
        vol   = calc_volatility(price_history, 10)
        if any(v is None for v in [rsi, sma3, sma8, ema8, ema21, mom3, mom8, vol]):
            return None
        bullish = sum([rsi > 55, sma3 > sma8, ema8 > ema21, mom3 > 0.01, mom8 > 0.02, vol < 0.05])
        bearish = sum([rsi < 45, sma3 < sma8, ema8 < ema21, mom3 < -0.01, mom8 < -0.02])
        cur = price_history[-1]
        if bullish >= 4 and cur < 0.80:
            confidence = 0.5 + (bullish / 6) * 0.4
            return TradeSignal(
                market=market, signal_type=SignalType.MOMENTUM, side=Side.BUY,
                confidence=confidence, yes_price_cents=int(cur * 100),
                edge=confidence - cur,
                reason=f"Bullish {bullish}/6 RSI={rsi:.1f}",
            )
        if bearish >= 3 and cur > 0.20:
            confidence = 0.5 + (bearish / 5) * 0.35
            return TradeSignal(
                market=market, signal_type=SignalType.MOMENTUM, side=Side.BUY,
                confidence=confidence, yes_price_cents=int((1 - cur) * 100),
                edge=confidence - (1 - cur),
                reason=f"Bearish {bearish}/5 RSI={rsi:.1f}",
            )
        return None

class StrategyEngine:
    def __init__(self, config, forecast_cache: dict = None):
        self.arb      = ArbitrageStrategy(
            min_spread=config.trading.min_arb_spread,
            fee_rate=config.trading.arb_fee_rate,
            min_liquidity=config.trading.arb_min_liquidity,
        )
        self.weather  = InternalWeatherStrategy(
            forecast_cache or {},
            min_confidence=config.trading.momentum_threshold,
        )
        self.momentum = MomentumStrategy(threshold=config.trading.momentum_threshold)

    def update_forecasts(self, forecast_cache: dict):
        self.weather.forecast_cache = forecast_cache

    def analyze(self, market: Market, price_history: List[float] = None) -> List[TradeSignal]:
        signals = []
        arb = self.arb.analyze(market)
        if arb:
            signals.append(arb)
        wx = self.weather.analyze(market)
        if wx:
            signals.append(wx)
        if price_history:
            mom = self.momentum.analyze(market, price_history)
            if mom:
                signals.append(mom)
        return signals

    async def generate_signals(
        self,
        all_markets: list,
        price_histories: dict,
        balance: float,
        **kwargs,
    ) -> list:
        signals = []
        for m in all_markets:
            ph = price_histories.get(m.slug + "-YES", [])
            sigs = self.analyze(m, ph if ph else None)
            # Size signals by balance * max_position_pct
            for s in sigs:
                s.size = round(balance * 0.04, 2)
            signals.extend(sigs)
        return signals
