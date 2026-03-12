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

from config import BotConfig, KALSHI_WEATHER_SERIES
from market_data import MarketDataFetcher
from strategies import StrategyEngine, is_weather_market, Side as StrategySide
from executor import TradeExecutor, Side as ExecSide
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
        self.strategy = StrategyEngine(config)
        self.weather = WeatherStrategy(
            min_confidence=config.trading.momentum_threshold,
            max_position_pct=config.trading.max_position_pct,
        )
        self.risk = RiskManager(config)
        self.executor = TradeExecutor(config)
        self._cycle_count = 0
        self._start_time = time.time()
        self._last_report = 0
        self._wx_markets_cache = []

    async def start(self):
        self.running = True
        self._start_time = time.time()
        self._print_banner()
        logger.info("Bot starting...")
        try:
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

    async def _cycle(self):
        tc = self.config.trading
        if self.risk.should_pause(self.executor.balance, tc.starting_balance):
            logger.warning("Risk pause active, sleeping 5min...")
            await asyncio.sleep(300)
            return
        try:
            all_m = await self.data.fetch_active_markets(limit=200)
            wx_m = [m for m in all_m if is_weather_market(m)]
            self._wx_markets_cache = wx_m

            logger.info(
                f"Scanned {len(all_m)} markets | {len(wx_m)} weather | "
                f"Open positions: {len(self.executor.open_positions)}"
            )

            if self._cycle_count < 3:
                for m in wx_m[:10]:
                    logger.info(f"  WX: {m.question} | Vol=${m.volume_24h:,.0f}")

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
                    wx_sigs = await self.weather.analyze(wx_m, self.executor.balance)
                    if wx_sigs:
                        logger.info(f"WeatherStrategy generated {len(wx_sigs)} signal(s)")
                    sigs.extend(wx_sigs)
                except Exception as e:
                    logger.warning(f"WeatherStrategy error: {e}", exc_info=True)

            prices = {}
            for m in all_m:
                for o in m.outcomes:
                    if o.token_id:
                        prices[o.token_id] = o.price

            if not sigs:
                if self._cycle_count % 18 == 0:
                    logger.info(f"No signals | Bal=${self.executor.balance:.2f} | Wx={len(wx_m)}")
                closed = await self.executor.evaluate_positions_with_data(prices, "", 0.0, {})
                for pos in closed:
                    self.risk.record_trade_result(pos.pnl)
                    logger.info(f"EXIT [{pos.exit_reason}]: {pos.market_slug} PnL=${pos.pnl:+.2f}")
                return

            logger.info(f"Generated {len(sigs)} signals")

            for s in sigs:
                if len(self.executor.open_positions) >= tc.max_concurrent_positions:
                    break
                if s.confidence < tc.momentum_threshold:
                    continue

                price_float = s.price
                yes_price_cents = int(price_float * 100)
                trade_value = self.executor.balance * tc.max_position_pct
                contracts = max(1, int(trade_value / max(price_float, 0.01)))
                cost = contracts * price_float
                if cost > self.executor.balance * 0.95:
                    logger.debug(f"SKIP (insufficient balance ${cost:.2f}): {s.market.question[:40]}")
                    continue

                exec_side = ExecSide.YES if getattr(s, "outcome", "Yes") == "Yes" else ExecSide.NO
                order = await self.executor._place_order(
                    market=s.market,
                    side=exec_side,
                    contracts=contracts,
                    yes_price_cents=yes_price_cents,
                )
                if order and order.status.value == "filled":
                    logger.info(
                        f"TRADE [{s.signal_type.value}]: {s.market.question[:50]} | "
                        f"{exec_side.value.upper()} x{contracts} @ {yes_price_cents}c | "
                        f"conf={s.confidence:.2f} | {s.reasoning}"
                    )
                await asyncio.sleep(0.5)

            closed = await self.executor.evaluate_positions_with_data(prices, "", 0.0, {})
            for pos in closed:
                self.risk.record_trade_result(pos.pnl)
                logger.info(f"EXIT [{pos.exit_reason}]: {pos.market_slug} PnL=${pos.pnl:+.2f}")

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)
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
  Cities:   {len(KALSHI_WEATHER_SERIES)} weather series tracked
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
        for slug, p in list(self.executor.open_positions.items())[:5]:
            unreal = (p.current_price - p.entry_price) * p.size
            logger.info(
                f"  POS: {p.question[:40]} | "
                f"Entry={p.entry_price:.3f} Cur={p.current_price:.3f} | "
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
        await self.weather.close()
        logger.info("Shutdown complete.")

async def main():
    from dotenv import load_dotenv
    load_dotenv()
    config = BotConfig.from_env()
    setup_logging(config)
    logger.info("Config loaded")
    bot = KalshiBot(config)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot._shutdown()))
        except NotImplementedError:
            pass
    await bot.start()

if __name__ == "__main__":
    asyncio.run(main())
