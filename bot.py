"""
bot.py  -  Kalshi Weather Trading Bot
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig, KALSHI_WEATHER_SERIES
from market_data import MarketDataFetcher
from strategies import StrategyEngine, is_weather_market
from executor import TradeExecutor
from risk_manager import RiskManager
from weather_strategy import WeatherStrategy


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
        # StrategyEngine needs forecast_cache; start with empty dict, update each cycle
        self._forecasts: dict = {}
        self.strategy = StrategyEngine(config, self._forecasts)
        self.weather = WeatherStrategy(
            min_confidence=0.55,
            max_position_pct=config.trading.max_position_pct,
        )
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
            # Fetch all active markets from Kalshi
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

            # Fetch orderbooks for top weather markets
            for m in wx_m[:20]:
                try:
                    await self.data.fetch_market_with_books(m)
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            # Build price history per token
            ph: dict = {}
            for m in all_m:
                for o in m.outcomes:
                    if o.token_id:
                        self.data.record_price(o.token_id, o.price)
                        ph[o.token_id] = self.data.get_price_history(o.token_id)

            # Update executor prices
            prices = {tid: hist[-1] for tid, hist in ph.items() if hist}
            self.executor.update_prices(prices)

            # Run arb + momentum strategies across all markets
            sigs = []
            for m in all_m:
                token_id = m.outcomes[0].token_id if m.outcomes else None
                history = ph.get(token_id, []) if token_id else []
                market_sigs = self.strategy.analyze(m, price_history=history if len(history) >= 22 else None)
                sigs.extend(market_sigs)

            # Run weather strategy separately (async, multi-source forecasts)
            if wx_m:
                try:
                    wx = await self.weather.analyze(wx_m, self.executor.balance)
                    if wx:
                        logger.info(f"WEATHER: {len(wx)} signals!")
                        sigs.extend(wx)
                    elif self._cycle_count % 6 == 0:
                        logger.info(f"WEATHER: {len(wx_m)} markets, no value trades")
                    # Cache forecasts so StrategyEngine.WeatherStrategy can use them too
                    if hasattr(self.weather, "fetcher"):
                        self._forecasts.update({
                            c: d for c, (_, d) in
                            self.weather.fetcher._forecast_cache.items()
                        })
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
                    if o and hasattr(o, "status") and str(o.status) == "filled":
                        logger.info(
                            f"TRADE: {s.outcome} | {s.market.question} | "
                            f"${s.size:.2f}@{s.price:.3f}"
                        )

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

    def _print_banner(self):
        mode = "DEMO" if self.config.kalshi.use_demo else "LIVE"
        dry = " [DRY RUN]" if self.config.trading.dry_run else ""
        city_count = len(KALSHI_WEATHER_SERIES)
        print(f"""
╔══════════════════════════════════════════════╗
║      Kalshi Weather Trading Bot              ║
║  Mode: {mode:<6}{dry:<12}                  ║
║  Cities: {city_count:<3} weather series tracked       ║
╔══════════════════════════════════════════════╝""")

    def _report(self):
        uptime = (time.time() - self._start_time) / 3600
        bal = self.executor.balance
        invested = self.executor.total_invested
        open_pos = len(self.executor.open_positions)
        logger.info(
            f"REPORT | Uptime={uptime:.1f}h | Balance=${bal:,.2f} | "
            f"Invested=${invested:,.2f} | OpenPos={open_pos} | Cycle={self._cycle_count}"
        )

    def _save(self):
        try:
            state = {
                "cycle": self._cycle_count,
                "balance": self.executor.balance,
                "open_positions": len(self.executor.open_positions),
                "total_invested": self.executor.total_invested,
                "trade_count": len(self.executor.trade_history),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            Path("logs").mkdir(exist_ok=True)
            with open("logs/state.json", "w") as f:
                import json
                json.dump(state, f, indent=2)
        except Exception:
            pass

    async def _shutdown(self):
        self.running = False
        logger.info("Shutting down...")
        if hasattr(self.weather, "close"):
            await self.weather.close()
        if hasattr(self.data, "close"):
            await self.data.close()
        logger.info("Shutdown complete.")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Weather Trading Bot")
    parser.add_argument("--balance", type=float, default=None,
                        help="Override starting balance for dry run")
    args = parser.parse_args()

    config = BotConfig.from_env()
    logger.info("Config loaded")

    if args.balance and config.trading.dry_run:
        config.trading.starting_balance = args.balance
        config.trading.max_position_size = args.balance * 0.10

    setup_logging(config)
    logger.info(
        f"Starting | dry_run={config.trading.dry_run} | "
        f"demo={config.kalshi.use_demo} | "
        f"balance=${config.trading.starting_balance:,.0f}"
    )

    bot = KalshiBot(config)

    import signal as _signal
    loop = asyncio.get_event_loop()
    for sig in (getattr(_signal, "SIGINT", None), getattr(_signal, "SIGTERM", None)):
        if sig:
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot._shutdown()))
            except (NotImplementedError, RuntimeError):
                pass

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
