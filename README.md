# Polymarket Autonomous Trading Bot

An autonomous trading bot for [Polymarket](https://polymarket.com) prediction markets that compounds profits daily. Based on the ClawdBot/MoltBot strategy that turned $100 into $347 overnight.

## What It Does

This bot runs 24/7 and makes money through three core strategies:

**1. Arbitrage (Risk-Free)** — Scans all markets for when YES + NO prices sum to less than $1.00. Buys both sides, guarantees profit on resolution regardless of outcome. This is the primary strategy and carries zero directional risk.

**2. BTC Momentum** — Trades Polymarket's short-duration "Bitcoin Up or Down" markets (5-min, 15-min) using technical analysis: RSI, moving average crossovers, Bollinger Bands, and resolution streak momentum. The bot buys the favored direction with high conviction.

**3. Mean Reversion** — Identifies markets where prices have deviated significantly from their moving average and bets on reversion to the mean.

**Compounding** — Profits are automatically reinvested. Position sizes scale with balance growth (capped at 3x to prevent over-leverage).

## How The Math Works

### Arbitrage
If YES costs $0.48 and NO costs $0.49 → combined = $0.97
Buy both for $0.97, guaranteed $1.00 payout = **$0.03 profit per share** (minus 2% fee).
With $50, placing $25 on each side = ~$0.75 risk-free profit per opportunity.

### Momentum on BTC 15-min Markets
The bot analyzes:
- Last 20+ BTC market resolutions for streak detection
- Real-time BTC price RSI (overbought/oversold signals)
- Short vs long moving average crossovers
- Bollinger Band positioning
- Weighted vote → direction + confidence score

Only trades when confidence exceeds 60%.

## Quick Start

### 1. Clone and Setup
```bash
git clone <this-repo>
cd polymarket-bot
```

### 2. Run in Paper Trading Mode (No Risk)
```bash
chmod +x run.sh
./run.sh --balance 50
```

This runs the bot with a simulated $50 balance. No real money, no wallet needed. Watch it scan markets, find opportunities, and simulate trades.

### 3. Run Live (Real Money)

**Prerequisites:**
- A Polygon wallet funded with USDC
- Python 3.9+

```bash
# Copy and configure environment
cp .env.example .env
# Edit .env with your private key and wallet address

# Run live
./run.sh --live --balance 50
```

⚠️ **Start small.** Use $30-50 to prove the bot works before scaling.

## Configuration

All settings are in `config.py`. Key parameters:

| Setting | Default | Description |
|---------|---------|-------------|
| `starting_balance` | $50 | Initial USDC balance |
| `max_position_pct` | 5% | Max % of balance per trade |
| `max_daily_loss_pct` | 10% | Pause trading if hit |
| `min_arb_spread` | 2.5% | Min arbitrage spread (covers 2% fee) |
| `momentum_threshold` | 60% | Min confidence for momentum trades |
| `stop_loss_pct` | 15% | Cut losses at this level |
| `take_profit_pct` | 25% | Take profits at this level |
| `scan_interval` | 10s | Time between market scans |
| `compound_profits` | true | Reinvest profits |

## Architecture

```
bot.py              Main loop: scan → analyze → risk check → execute → manage
├── config.py       All configurable parameters
├── market_data.py  Fetches from Gamma API + CLOB API + CoinGecko
├── strategies.py   Arbitrage scanner + Momentum engine + Mean reversion
├── executor.py     Order placement (paper + live) + position tracking
├── risk_manager.py Position limits, daily loss caps, exposure controls
└── dashboard.py    Web UI for monitoring (http://localhost:8080)
```

## Risk Management

The bot enforces multiple layers of protection:

- **Position size cap**: Max 5% of balance per directional trade, 10% for arbitrage
- **Concurrent limit**: Max 5 open positions at any time
- **Total exposure cap**: Never invest more than 50% of balance
- **Daily loss limit**: Pauses all trading if daily losses exceed 10%
- **Critical stop**: Halts completely if balance drops below 50% of starting
- **Stop-loss**: Individual positions cut at 15% loss
- **Take-profit**: Locks in gains at 25%
- **No duplicates**: Won't re-enter the same market twice per day (except arb)

## Dashboard

The bot includes a web dashboard at `http://localhost:8080` showing:
- Current balance and PnL
- Win rate and trade count
- Open positions
- Recent trade log with reasoning

Auto-refreshes every 15 seconds.

## File Structure

```
polymarket-bot/
├── bot.py              # Main entry point
├── config.py           # Configuration
├── market_data.py      # Market data fetching
├── strategies.py       # Trading strategies
├── executor.py         # Trade execution
├── risk_manager.py     # Risk management
├── dashboard.py        # Web monitoring UI
├── run.sh              # Quick start script
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template
├── logs/               # Trading logs (auto-created)
└── state/              # Bot state for recovery (auto-created)
```

## Wallet Setup for Live Trading

1. **Create a dedicated wallet** — Don't use your main wallet. Create a fresh one just for this bot.

2. **Fund with USDC on Polygon** — Bridge USDC to Polygon network. You need USDC (not native MATIC/POL) for trading, plus a small amount of POL for gas (~$0.50 is enough for thousands of trades).

3. **Set token approvals** — The bot needs permission to trade on Polymarket's exchange contracts. The py-clob-client handles this, but on first run you may need to approve the USDC and CTF contracts.

4. **Configure .env** — Set `POLYMARKET_PK` to your wallet's private key and `DRY_RUN=false`.

## Disclaimer

⚠️ **This bot trades real money when run in live mode. Use at your own risk.**

- Crypto trading carries significant financial risk
- Past performance does not guarantee future results
- Arbitrage opportunities may be fleeting and competitive
- Start with a small amount you can afford to lose
- The 2% Polymarket fee eats into thin arbitrage margins
- Market liquidity can dry up, causing slippage
- This is not financial advice

## Credits

Inspired by the ClawdBot/MoltBot strategy shared by [@xmayeth](https://x.com/xmayeth), which demonstrated autonomous Polymarket trading with $100 turning into $347 overnight through a combination of arbitrage detection, momentum trading on BTC 15-minute markets, and automated compounding.
"# botter" 
