"""
bot.py  -  Polymarket Weather Trading Bot
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
from weather_strategy import WeatherStrategy, AIRPORTS


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
    root = logging.getLogger("polymarket_bot")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)
    root.addHandler(_ring)


logger = logging.getLogger("polymarket_bot.main")


class PolymarketBot:
    def __init__(self, config):
        self.config = config
        self.running = False
        self.data = MarketDataFetcher(
            gamma_host=config.api.gamma_host, clob_host=config.api.clob_host)
        self.strategy = StrategyEngine(config)
        self.weather = WeatherStrategy(
            min_confidence=0.55, max_position_pct=config.trading.max_position_pct)
        self.risk = RiskManager(config)
        self.executor = TradeExecutor(config, clob_client=self._init_clob())
        self._cycle_count = 0
        self._start_time = time.time()
        self._last_report = 0
        self._forecasts = {}
        self._pnl_history = []
        self._wx_markets_cache = []
        self._weather_refresh_interval = 60

    def _init_clob(self):
        if self.config.dry_run:
            logger.info("DRY RUN MODE")
            return None
        if not self.config.wallet.private_key:
            self.config.dry_run = True
            return None
        try:
            from py_clob_client.client import ClobClient
            c = ClobClient(
                self.config.api.clob_host,
                key=self.config.wallet.private_key,
                chain_id=self.config.api.chain_id,
                signature_type=self.config.wallet.signature_type,
                funder=self.config.wallet.funder_address or None,
            )
            c.set_api_creds(c.create_or_derive_api_creds())
            logger.info("LIVE MODE")
            return c
        except Exception as e:
            logger.error(f"CLOB failed: {e}")
            self.config.dry_run = True
            return None

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

    async def _cycle(self):
        tc = self.config.trading
        if self.risk.should_pause(self.executor.balance, tc.starting_balance):
            await asyncio.sleep(300)
            return
        try:
            all_m = []
            seen = set()
            for pg in range(5):
                batch = await self.data.fetch_active_markets(
                    limit=500, order="volume24hr", ascending=False, offset=pg * 500)
                for m in batch:
                    if m.condition_id not in seen:
                        all_m.append(m)
                        seen.add(m.condition_id)
                if len(batch) < 500:
                    break
                await asyncio.sleep(0.3)

            wx_m = [m for m in all_m if is_weather_market(m)]
            self._wx_markets_cache = wx_m
            logger.info(
                f"Scanned {len(all_m)} | {len(wx_m)} weather | "
                f"Open: {len(self.executor.open_positions)}"
            )

            if self._cycle_count < 3:
                for m in wx_m[:10]:
                    logger.info(f"  WX: {m.question} | Vol=${m.volume_24h:,.0f}")

            for m in wx_m[:20] + all_m[:20]:
                try:
                    await self.data.fetch_market_with_books(m)
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            prices = {}
            for m in all_m:
                for o in m.outcomes:
                    if o.token_id:
                        prices[o.token_id] = o.price
            self.executor.update_prices(prices)

            ph = {}
            for m in all_m[:500]:
                for o in m.outcomes:
                    if o.token_id:
                        self.data.record_price(o.token_id, o.price)
                        ph[o.token_id] = self.data.get_price_history(o.token_id)

            sigs = await self.strategy.generate_signals(
                all_markets=all_m, crypto_markets=[], btc_prices=[],
                price_histories=ph, balance=self.executor.balance)

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
        print(f"""
================================================================
        POLYMARKET WEATHER TRADING BOT
================================================================
  Mode:     {mode} | Balance: ${self.config.trading.starting_balance:.2f}
  Scan:     Every {self.config.trading.scan_interval}s (2500 markets)
  Report:   Every 5 min | Max positions: {self.config.trading.max_concurrent_positions}
  Sources:  NOAA (US) + Open-Meteo (worldwide)
================================================================
""")


    async def _fast_weather_loop(self):
        """Re-fetch weather forecasts every 60s independently of main scan cycle."""
        await asyncio.sleep(30)  # initial delay to let first cycle run
        while self.running:
            try:
                if hasattr(self.weather, 'fetcher') and self._wx_markets_cache:
                    self.weather.fetcher._forecast_cache.clear()
                    logger.debug("WEATHER REFRESH: forecast cache cleared, re-fetching...")
                    await self.weather.analyze(self._wx_markets_cache, self.executor.balance)
                    logger.debug("WEATHER REFRESH: forecasts updated")
            except Exception as e:
                logger.debug(f"WEATHER REFRESH error (non-fatal): {e}")
            await asyncio.sleep(self._weather_refresh_interval)

    def _report(self):
        s = self.executor.get_summary()
        h = (time.time() - self._start_time) / 3600
        rlz = s["total_pnl"]
        unr = s.get("unrealized_pnl", 0)
        tot = rlz + unr

        def f(v):
            return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

        pct = s["pnl_pct"]
        ps = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
        all_open = self.executor.open_positions
        pos_text = ""
        for p in all_open:
            cp = self.executor._current_prices.get(p.token_id, p.entry_price)
            up = p.unrealized_pnl(cp)
            upp = p.unrealized_pnl_pct(cp)
            pos_text += (
                f"\n  #{p.id:<3d} {p.outcome:3s} | "
                f"{f(up):>10s} ({upp:+.0%}) | "
                f"{p.age_str:>5s} | {p.question}"
            )
        if not pos_text:
            pos_text = "\n  No open positions."

        fc_text = ""
        for city in sorted(self._forecasts.keys()):
            fc = self._forecasts[city]
            tf = fc.get("temp_f", 0)
            tc_v = fc.get("temp_c", 0)
            src = "+".join(fc.get("sources", []))
            st = fc.get("station", AIRPORTS.get(city.lower(), {}).get("station", city))
            fc_text += f"\n  {st:48s} {tf:3.0f}F / {tc_v:2.0f}C  ({src})"
        if not fc_text:
            fc_text = "\n  Waiting for first forecast cycle..."

        print(f"""
============ REPORT ({time.strftime('%H:%M:%S')}) ============
  Balance: ${s['starting_balance']:.2f} -> ${s['balance']:.2f} ({ps})
  Realized: {f(rlz)} | Unrealized: {f(unr)} | Total: {f(tot)}
  Trades: {s['total_trades']} | Win rate: {s['win_rate']:.0f}%
  Positions ({len(all_open)} open){pos_text}
  Forecasts{fc_text}
  Uptime: {h:.1f}h | Cycles: {self._cycle_count}
==============================================
""")

    def _save(self):
        Path("state").mkdir(exist_ok=True)
        try:
            s = self.executor.get_summary()
            uptime_secs = int(time.time() - self._start_time)

            total_pnl = s["total_pnl"] + s.get("unrealized_pnl", 0)
            self._pnl_history.append(round(total_pnl, 4))
            if len(self._pnl_history) > 120:
                self._pnl_history = self._pnl_history[-120:]

            positions = []
            for p in self.executor.open_positions:
                cp = self.executor._current_prices.get(p.token_id, p.entry_price)
                upnl = p.unrealized_pnl(cp)
                upnl_pct = p.unrealized_pnl_pct(cp) * 100
                positions.append({
                    "id": p.id,
                    "question": p.question,
                    "outcome": p.outcome,
                    "entry_price": round(p.entry_price, 4),
                    "current_price": round(cp, 4),
                    "size": round(p.size, 2),
                    "cost": round(p.cost, 2),
                    "unrealized_pnl": round(upnl, 4),
                    "unrealized_pnl_pct": round(upnl_pct, 2),
                    "confidence": round(p.confidence, 4),
                    "forecast_temp": p.forecast_temp,
                    "forecast_unit": p.forecast_unit,
                    "temp_range_low": p.temp_range_low,
                    "temp_range_high": p.temp_range_high,
                    "city": p.city,
                    "age_str": p.age_str,
                    "signal_type": p.signal_type,
                })

            forecasts = {}
            for city, fc in self._forecasts.items():
                forecasts[city] = {
                    "temp_f": fc.get("temp_f", 0),
                    "temp_c": fc.get("temp_c", 0),
                    "sources": fc.get("sources", []),
                    "station": fc.get(
                        "station",
                        AIRPORTS.get(city.lower(), {}).get("station", city)
                    ),
                }

            state = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": "LIVE" if not self.config.dry_run else "PAPER",
                "uptime_secs": uptime_secs,
                "cycle_count": self._cycle_count,
                "summary": s,
                "pnl_history": self._pnl_history,
                "positions": positions,
                "forecasts": forecasts,
                "log_lines": _ring.lines(),
                "trade_history": self.executor.trade_history[-200:],
            }

            tmp = Path("state/bot_state.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(state, fp, indent=2, default=str)
            tmp.replace(Path("state/bot_state.json"))

        except Exception as e:
            logger.warning(f"_save failed: {e}")

    async def _shutdown(self):
        logger.info("Shutting down...")
        self.running = False
        await self.data.close()
        await self.weather.close()
        self._save()
        self._report()


def main():
    config = BotConfig.from_env()
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--balance", type=float, default=None)
    p.add_argument("--live", action="store_true")
    p.add_argument("--scan-interval", type=int, default=None)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--sell", type=int, default=None)
    p.add_argument("--sell-all", action="store_true")
    p.add_argument("--positions", action="store_true")
    args = p.parse_args()

    if args.positions:
        try:
            with open("state/bot_state.json") as fp:
                state = json.load(fp)
            print(f"\nBalance: ${state['summary']['balance']:.2f}")
            print(f"Open positions: {state['summary']['open_positions']}")
            for pos in state.get("positions", []):
                upnl = pos.get("unrealized_pnl", 0)
                sign = "+" if upnl >= 0 else ""
                print(
                    f"  #{pos['id']:<3d} {pos['outcome']:3s} | "
                    f"${pos['cost']:.2f} | {sign}${upnl:.2f} uPnL | "
                    f"{pos['age']:>5s} | {pos['question']}"
                )
        except Exception as e:
            print(f"No state file found. Run the bot first. ({e})")
        return

    if args.balance:
        config.trading.starting_balance = args.balance
    if args.live:
        config.dry_run = False
    if args.scan_interval:
        config.trading.scan_interval = args.scan_interval
    config.logging.log_level = args.log_level

    setup_logging(config)
    bot = PolymarketBot(config)
    loop = asyncio.new_event_loop()
    signal.signal(signal.SIGINT, lambda s, f: setattr(bot, "running", False))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(bot, "running", False))
    try:
        loop.run_until_complete(bot.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
