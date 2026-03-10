"""
Dashboard - Simple HTTP server for monitoring bot performance.
Run alongside the bot to view PnL, trades, and positions in your browser.
"""

import json
import logging
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

logger = logging.getLogger("polymarket_bot.dashboard")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot Dashboard</title>
<meta http-equiv="refresh" content="15">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', monospace;
    background: #0a0a0f;
    color: #e0e0e0;
    padding: 20px;
  }
  .header {
    text-align: center;
    padding: 20px;
    border-bottom: 1px solid #1a1a2e;
    margin-bottom: 20px;
  }
  .header h1 { color: #00ff88; font-size: 24px; }
  .header .mode { color: #ff6b6b; font-size: 14px; margin-top: 5px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 15px;
    margin-bottom: 20px;
  }
  .card {
    background: #12121f;
    border: 1px solid #1a1a2e;
    border-radius: 8px;
    padding: 15px;
  }
  .card .label { color: #888; font-size: 12px; text-transform: uppercase; }
  .card .value { font-size: 28px; font-weight: bold; margin-top: 5px; }
  .positive { color: #00ff88; }
  .negative { color: #ff4444; }
  .neutral { color: #ffaa00; }
  .trades-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
  }
  .trades-table th, .trades-table td {
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid #1a1a2e;
    font-size: 13px;
  }
  .trades-table th { color: #888; font-weight: normal; text-transform: uppercase; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: bold;
  }
  .badge-arb { background: #1a3a2a; color: #00ff88; }
  .badge-mom { background: #3a2a1a; color: #ffaa00; }
  .badge-mr { background: #2a1a3a; color: #aa88ff; }
  .footer { text-align: center; color: #444; font-size: 12px; margin-top: 30px; }
</style>
</head>
<body>
  <div class="header">
    <h1>POLYMARKET BOT</h1>
    <div class="mode">{mode} | Updated: {timestamp}</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label">Balance</div>
      <div class="value {balance_class}">${balance}</div>
    </div>
    <div class="card">
      <div class="label">Total PnL</div>
      <div class="value {pnl_class}">${pnl}</div>
    </div>
    <div class="card">
      <div class="label">PnL %</div>
      <div class="value {pnl_class}">{pnl_pct}%</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value">{win_rate}%</div>
    </div>
    <div class="card">
      <div class="label">Total Trades</div>
      <div class="value">{total_trades}</div>
    </div>
    <div class="card">
      <div class="label">Open Positions</div>
      <div class="value neutral">{open_positions}</div>
    </div>
    <div class="card">
      <div class="label">Available</div>
      <div class="value">${available}</div>
    </div>
    <div class="card">
      <div class="label">Cycles</div>
      <div class="value">{cycles}</div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 15px;">
    <div class="label">Recent Trades</div>
    <table class="trades-table">
      <tr>
        <th>Time</th>
        <th>Type</th>
        <th>Market</th>
        <th>Side</th>
        <th>Price</th>
        <th>Cost</th>
        <th>Confidence</th>
      </tr>
      {trade_rows}
    </table>
  </div>

  <div class="footer">
    Auto-refreshes every 15 seconds | Polymarket Autonomous Trading Bot
  </div>
</body>
</html>"""


def _get_state() -> dict:
    """Load bot state from file."""
    state_path = Path("state/bot_state.json")
    if state_path.exists():
        try:
            with open(state_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "summary": {
            "balance": 0, "total_pnl": 0, "pnl_pct": 0,
            "win_rate": 0, "total_trades": 0, "open_positions": 0,
            "available": 0,
        },
        "trade_history": [],
        "cycle_count": 0,
        "config": {"dry_run": True},
    }


def _render_dashboard() -> str:
    """Render dashboard HTML with current state."""
    state = _get_state()
    s = state.get("summary", {})
    trades = state.get("trade_history", [])[-20:]  # Last 20
    trades.reverse()

    pnl = s.get("total_pnl", 0)
    pnl_class = "positive" if pnl >= 0 else "negative"
    balance_class = "positive" if s.get("balance", 0) >= s.get("starting_balance", 50) else "negative"

    mode = "DRY RUN" if state.get("config", {}).get("dry_run", True) else "LIVE"

    # Build trade rows
    rows = ""
    for t in trades:
        sig_type = t.get("signal_type", "?")
        badge_class = {
            "arbitrage": "badge-arb",
            "momentum": "badge-mom",
            "mean_revert": "badge-mr",
        }.get(sig_type, "badge-mom")

        ts = t.get("timestamp", 0)
        time_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"

        rows += f"""<tr>
          <td>{time_str}</td>
          <td><span class="badge {badge_class}">{sig_type[:3].upper()}</span></td>
          <td>{t.get('market', '?')[:50]}</td>
          <td>{t.get('side', '?')}</td>
          <td>${t.get('price', 0):.4f}</td>
          <td>${t.get('cost', 0):.2f}</td>
          <td>{t.get('confidence', 0):.0%}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#444">No trades yet</td></tr>'

    return DASHBOARD_HTML.format(
        mode=mode,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        balance=f"{s.get('balance', 0):.2f}",
        balance_class=balance_class,
        pnl=f"{pnl:+.2f}",
        pnl_class=pnl_class,
        pnl_pct=f"{s.get('pnl_pct', 0):+.1f}",
        win_rate=f"{s.get('win_rate', 0):.0f}",
        total_trades=s.get("total_trades", 0),
        open_positions=s.get("open_positions", 0),
        available=f"{s.get('available', 0):.2f}",
        cycles=state.get("cycle_count", 0),
        trade_rows=rows,
    )


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_render_dashboard().encode())
        elif self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(_get_state()).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_dashboard(port: int = 8080):
    """Start the dashboard server in a background thread."""
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"📊 Dashboard running at http://localhost:{port}")
    return server
