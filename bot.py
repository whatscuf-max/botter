"""
bot.py  -  Kalshi Weather Trading Bot
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig
from market_data import MarketDataFetcher
from strategies import StrategyEngine, is_weather_market
from executor import TradeExecutor
from risk_manager import RiskManager
from weather_strategy import WeatherStrategy, KALSHI_WEATHER_SERIES

class RingBufferHandler(logging.Handler):
    def __init__(self, capacity=200):
        super().__init__()
        self._buf = deque(maxlen=capacity)

    def emit(self, record):
        self._buf.append({
            "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": self.format(record),
        })

    def lines(self):
        return list(self._buf)

_ring = RingBufferHandler(200)
_ring.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S"
))

def setup_logging(config):
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_dir / config.logging.log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    try:
        ch = logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        )
    except Exception:
        ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, config.logging.log_level))
    ch.setFormatter(fmt)
    root = logging.getLogger("kalshi_bot")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    root.addHandler(_ring)

logger = logging.getLogger("kalshi_bot.main")

class KalshiBot:
    def __init__(self, config):
        self.config = config
        self.running = False
        self.data = MarketDataFetcher(config)
        self._forecasts: dict = {}
        self.strategy = StrategyEngine(config, self._forecasts)
        self.weather = WeatherStrategy(
            min_confidence=0.55, max_position_pct=config.trading.max_position_pct)
        self.risk = RiskManager(config)
        self.executor = TradeExecutor(config)
        self._cycle_count = 0
        self._start_time = time.time()
        self._last_report = 0
        self._pnl_history = []
        self._wx_markets_cache = []
        self._weather_refresh_interval = 60

    async def start(self):
        self.running = True
        self._start_time = time.time()
        self._print_banner()
        logger.info("Bot starting...")
        try:
            asyncio.create_task(self._fast_weather_loop())
            while self.running:
                await self._cycle()
                self._cycle_count += 1
                if time.time() - self._last_report > 300:
                    self._report()
                    self._last_report = time.time()
                self._save()
                await asyncio.sleep(self.config.trading.scan_interval)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.critical(f"Fatal: {e}", exc_info=True)
        finally:
            await self._shutdown()

    async def _fast_weather_loop(self):
        """Refresh weather forecasts more frequently than the main cycle."""
        while self.running:
            try:
                if self._wx_markets_cache:
                    wx = await self.weather.analyze(
                        self._wx_markets_cache, self.executor.balance)
                    if wx:
                        logger.debug(f"Fast weather loop: {len(wx)} signals refreshed")
            except Exception as e:
                logger.debug(f"Fast weather loop err: {e}")
            await asyncio.sleep(self._weather_refresh_interval)

    async def _cycle(self):
        tc = self.config.trading
        if self.risk.should_pause(self.executor.balance, tc.starting_balance):
            await asyncio.sleep(300)
            return
        try:
            # Fetch weather markets from Kalshi
            all_m = await self.data.fetch_active_markets(limit=200)
            wx_m = [m for m in all_m if is_weather_market(m)]
            self._wx_markets_cache = wx_m

            logger.info(
                f"Scanned {len(all_m)} | {len(wx_m)} weather | "
                f"Open: {len(self.executor.open_positions)}"
            )

            if self._cycle_count < 3:
                for m in wx_m[:10]:
                    logger.info(f"  WX: {m.question} | Vol=${m.volume_24h:,.0f}")

            # Fetch orderbooks for top markets
            for m in wx_m[:20]:
                try:
                    await self.data.fetch_market_with_books(m)
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            # Build price map
            prices = {}
            for m in all_m:
                for o in m.outcomes:
                    if o.token_id:
                        prices[o.token_id] = o.price
            self.executor.update_prices(prices)

            # Build price history
            ph = {}
            for m in all_m:
                for o in m.outcomes:
                    if o.token_id:
                        self.data.record_price(o.token_id, o.price)
                        ph[o.token_id] = self.data.get_price_history(o.token_id)

            sigs = []
            for m in all_m:
                ph_m = ph.get(m.outcomes[0].token_id, []) if m.outcomes else []
                sigs.extend(self.strategy.analyze(m, ph_m))

            if wx_m:
                try:
                    wx = await self.weather.analyze(wx_m, self.executor.balance)
                    if wx:
                        logger.info(f"WEATHER: {len(wx)} signals!")
                        sigs.extend(wx)
                    elif self._cycle_count % 6 == 0:
                        logger.info(f"WEATHER: {len(wx_m)} markets, no value trades")
                    if hasattr(self.weather, "fetcher"):
                        self._forecasts = {
                            c: d for c, (_, d) in
                            self.weather.fetcher._forecast_cache.items()
                        }
                except Exception as e:
                    logger.error(f"Weather err: {e}", exc_info=True)

            if sigs:
                ok = self.risk.filter_signals(
                    signals=sigs,
                    balance=self.executor.balance,
                    available_balance=self.executor.available_balance,
                    open_position_count=len(self.executor.open_positions),
                    total_invested=self.executor.total_invested,
                    trade_history=self.executor.trade_history,
                )
                for s in ok:
                    s.size = self.risk.calculate_compound_size(
                        s.size, self.executor.balance, tc.starting_balance)
                    o = await self.executor.execute_signal(s)
                    if o and o.status.value == "filled":
                        logger.info(
                            f"TRADE: {s.outcome} | {s.market.question} | "
                            f"${s.size:.2f}@{s.price:.3f}"
                        )
                    await asyncio.sleep(1)
            elif self._cycle_count % 18 == 0:
                logger.info(
                    f"Scanning | Bal=${self.executor.balance:.2f} | Wx={len(wx_m)}")

            closed = await self.executor.evaluate_positions_with_data(
                prices, "", 0, self._forecasts)
            for p in closed:
                ps = f"+${p.pnl:.2f}" if p.pnl >= 0 else f"-${abs(p.pnl):.2f}"
                logger.info(f"DATA EXIT: {p.question} | {ps} | {p.exit_reason}")

        except Exception as e:
            logger.error(f"Cycle err: {e}", exc_info=True)
            await asyncio.sleep(5)

    def _print_banner(self):
        mode = "DRY RUN" if self.config.dry_run else "LIVE"
        demo = " [DEMO]" if self.config.kalshi.use_demo else ""
        print(f"""
================================================================
        KALSHI WEATHER TRADING BOT{demo}
================================================================
  Mode:     {mode} | Balance: ${self.config.trading.starting_balance:.2f}
  Scan:     Every {self.config.trading.scan_interval}s
  Report:   Every 5 min | Max positions: {self.config.trading.max_concurrent_positions}
  Sources:  NOAA (US NWS stations) + Kalshi REST API v2
  Cities:   {len(KALSHI_WEATHER_SERIES)} markets tracked
================================================================
""")

    def _report(self):
        up = time.time() - self._start_time
        h, m = divmod(int(up) // 60, 60)
        logger.info(
            f"=== REPORT | Up={h}h{m}m | "
            f"Cycles={self._cycle_count} | "
            f"Bal=${self.executor.balance:.2f} | "
            f"Positions={len(self.executor.open_positions)} ==="
        )
        for p in list(self.executor.open_positions.values())[:5]:
            cur = self.executor._prices.get(p.token_id, p.entry_price)
            unreal = (cur - p.entry_price) * p.size
            logger.info(
                f"  POS: {p.question[:40]} | "
                f"Entry={p.entry_price:.3f} Cur={cur:.3f} | "
                f"P&L={unreal:+.2f}"
            )

    def _save(self):
        try:
            state = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "balance": self.executor.balance,
                "open_positions": len(self.executor.open_positions),
                "cycles": self._cycle_count,
            }
            with open("state.json", "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    async def _shutdown(self):
        self.running = False
        logger.info("Shutting down...")
        await self.data.close()
        logger.info("Shutdown complete.")

def handle_signal(bot, loop):
    bot.running = False
    loop.stop()

async def main():
    from dotenv import load_dotenv
    load_dotenv()

    config = BotConfig.from_env()
    setup_logging(config)
    logger.info("Config loaded")

    bot = KalshiBot(config)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal, bot, loop)

    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
