"""
Polymarket Weather Trading Bot
Sell commands: stop bot with Ctrl+C, then run:
  python -c "from executor import ...; sell(3)" 
Or use the built-in command mode.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import BotConfig
from market_data import MarketDataFetcher
from strategies import StrategyEngine, is_weather_market
from executor import TradeExecutor
from risk_manager import RiskManager
from weather_strategy import WeatherStrategy, AIRPORTS


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
            open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)
        )
    except Exception:
        ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, config.logging.log_level))
    ch.setFormatter(fmt)
    root = logging.getLogger("polymarket_bot")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)


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
        self._forecasts: dict = {}

    def _init_clob(self):
        if self.config.dry_run:
            logger.info("DRY RUN MODE")
            return None
        if not self.config.wallet.private_key:
            self.config.dry_run = True
            return None
        try:
            from py_clob_client.client import ClobClient
            c = ClobClient(self.config.api.clob_host,
                key=self.config.wallet.private_key,
                chain_id=self.config.api.chain_id,
                signature_type=self.config.wallet.signature_type,
                funder=self.config.wallet.funder_address or None)
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
            # Fetch 5 pages
            all_m = []
            seen = set()
            for pg in range(5):
                batch = await self.data.fetch_active_markets(
                    limit=500, order="volume24hr", ascending=False, offset=pg*500)
                for m in batch:
                    if m.condition_id not in seen:
                        all_m.append(m)
                        seen.add(m.condition_id)
                if len(batch) < 500: break
                await asyncio.sleep(0.3)

            wx_m = [m for m in all_m if is_weather_market(m)]
            logger.info(f"Scanned {len(all_m)} | {len(wx_m)} weather | Open: {len(self.executor.open_positions)}")

            if self._cycle_count < 3:
                for m in wx_m[:10]:
                    logger.info(f"  WX: {m.question} | Vol=${m.volume_24h:,.0f}")

            for m in wx_m[:20] + all_m[:20]:
                try: await self.data.fetch_market_with_books(m)
                except: pass
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
                all_markets=all_m, crypto_markets=[], btc_prices=[], price_histories=ph,
                balance=self.executor.balance)

            if wx_m:
                try:
                    wx = await self.weather.analyze(wx_m, self.executor.balance)
                    if wx:
                        logger.info(f"WEATHER: {len(wx)} signals!")
                        sigs.extend(wx)
                    elif self._cycle_count % 6 == 0:
                        logger.info(f"WEATHER: {len(wx_m)} markets, no value trades")
                    if hasattr(self.weather, 'fetcher'):
                        self._forecasts = {c: d for c, (_, d) in self.weather.fetcher._forecast_cache.items()}
                except Exception as e:
                    logger.error(f"Weather err: {e}", exc_info=True)

            if sigs:
                ok = self.risk.filter_signals(
                    signals=sigs, balance=self.executor.balance,
                    available_balance=self.executor.available_balance,
                    open_position_count=len(self.executor.open_positions),
                    total_invested=self.executor.total_invested,
                    trade_history=self.executor.trade_history)
                for s in ok:
                    s.size = self.risk.calculate_compound_size(
                        s.size, self.executor.balance, tc.starting_balance)
                    o = await self.executor.execute_signal(s)
                    if o and o.status.value == "filled":
                        logger.info(f"TRADE: {s.outcome} | {s.market.question} | ${s.size:.2f}@{s.price:.3f}")
                    await asyncio.sleep(1)
            elif self._cycle_count % 18 == 0:
                logger.info(f"Scanning | Bal=${self.executor.balance:.2f} | Wx={len(wx_m)}")

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
  Forecast: Refreshes every 5 min from airport stations
  Sources:  NOAA (US) + Open-Meteo (worldwide)
  Risk:     Only buy when forecast DISAGREES with market
            Never buy YES or NO above 75% (no edge)
  Exit:     Hold to settlement, exit on 3+ degree forecast shift
================================================================
  SELL COMMANDS: Stop bot (Ctrl+C) then run:
    python -c "exec(open('sell_cmd.py').read())" positions
    python -c "exec(open('sell_cmd.py').read())" sell 3
    python -c "exec(open('sell_cmd.py').read())" sell all
================================================================
""")

    def _report(self):
        s = self.executor.get_summary()
        h = (time.time() - self._start_time) / 3600
        rlz = s['total_pnl']
        unr = s.get('unrealized_pnl', 0)
        tot = rlz + unr

        def f(v): return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
        pct = s['pnl_pct']
        ps = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"

        # ALL positions - NO CAP
        all_open = self.executor.open_positions
        total_open = len(all_open)

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

        # Forecasts with airport names
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
  BALANCE
    Start: ${s['starting_balance']:.2f} | Now: ${s['balance']:.2f} ({ps})
    Invested: ${s['total_invested']:.2f} | Free: ${s['available']:.2f}

  PNL
    Realized:   {f(rlz)} ({s['total_trades']} trades, {s['win_rate']:.0f}%W)
    Unrealized: {f(unr)} ({total_open} open)
    Total:      {f(tot)} | Per hr: {f(rlz / max(h, 0.01))}

  POSITIONS ({total_open} open){pos_text}

  FORECASTS (Airport Stations - live every 5 min){fc_text}

  Uptime: {h:.1f}h | Cycles: {self._cycle_count}
==============================================""")

    def _save(self):
        Path("state").mkdir(exist_ok=True)
        try:
            # Save state including executor for sell commands
            state = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "summary": self.executor.get_summary(),
                "trade_history": self.executor.trade_history[-200:],
                "cycle_count": self._cycle_count,
                "positions": [
                    {"id": p.id, "question": p.question, "outcome": p.outcome,
                     "entry_price": p.entry_price, "size": p.size, "cost": p.cost,
                     "city": p.city, "age": p.age_str}
                    for p in self.executor.open_positions
                ],
            }
            with open("state/bot_state.json", "w", encoding="utf-8") as fp:
                json.dump(state, fp, indent=2, default=str)
        except Exception:
            pass

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
    # Sell commands
    p.add_argument("--sell", type=int, default=None, help="Sell position by ID")
    p.add_argument("--sell-all", action="store_true", help="Sell all positions")
    p.add_argument("--positions", action="store_true", help="Show positions")
    args = p.parse_args()

    if args.positions:
        # Quick view mode - just show positions from saved state
        try:
            with open("state/bot_state.json") as fp:
                state = json.load(fp)
            print(f"\nBalance: ${state['summary']['balance']:.2f}")
            print(f"Open positions: {state['summary']['open_positions']}")
            for pos in state.get("positions", []):
                print(f"  #{pos['id']:<3d} {pos['outcome']:3s} | ${pos['cost']:.2f} | {pos['age']:>5s} | {pos['question']}")
        except Exception as e:
            print(f"No state file found. Run the bot first. ({e})")
        return

    if args.balance: config.trading.starting_balance = args.balance
    if args.live: config.dry_run = False
    if args.scan_interval: config.trading.scan_interval = args.scan_interval
    config.logging.log_level = args.log_level

    setup_logging(config)
    bot = PolymarketBot(config)
    loop = asyncio.new_event_loop()
    signal.signal(signal.SIGINT, lambda s, f: setattr(bot, 'running', False))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(bot, 'running', False))
    try:
        loop.run_until_complete(bot.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
