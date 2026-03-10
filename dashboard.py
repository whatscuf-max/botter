"""
dashboard.py  -  Polymarket Weather Bot  |  Bloomberg-style Textual TUI
Run:  python dashboard.py          (attaches to live bot via shared state file)
      python dashboard.py --demo   (demo mode with fake data)
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Label, Log, Static
from rich.text import Text

# ---------------------------------------------------------------------------
# STATE FILE  (bot.py writes this every cycle)
# ---------------------------------------------------------------------------
STATE_FILE = Path("bot_state.json")

# ---------------------------------------------------------------------------
# DEMO DATA
# ---------------------------------------------------------------------------
_CITIES = ["New York", "Seattle", "London", "Tokyo", "Miami", "Chicago", "Paris", "Toronto"]
_OUTCOMES = ["Yes", "No"]
_SIGNALS = ["WEATHER_EDGE", "ARBITRAGE"]


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).strftime("%m/%d")


def _demo_positions(n: int = 6) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        city = _CITIES[i % len(_CITIES)]
        lo = random.randint(38, 62)
        hi = lo + random.randint(1, 3)
        entry = round(random.uniform(0.25, 0.75), 4)
        cur = round(max(0.01, min(0.99, entry + random.uniform(-0.12, 0.18))), 4)
        cost = round(random.uniform(8, 40), 2)
        shares = round(cost / entry, 1)
        upnl = round((cur - entry) * shares, 2)
        fc = round(random.uniform(lo - 4, hi + 4), 1)
        out.append({
            "id": i,
            "question": f"Will {city} high be {lo}-{hi}F on {_tomorrow()}?",
            "city": city,
            "outcome": _OUTCOMES[i % 2],
            "entry_price": entry,
            "current_price": cur,
            "size": shares,
            "cost": cost,
            "unrealized_pnl": upnl,
            "confidence": round(random.uniform(0.55, 0.92), 2),
            "forecast_temp": fc,
            "temp_range_low": lo,
            "temp_range_high": hi,
            "forecast_unit": "F",
            "signal_type": _SIGNALS[i % 2],
            "age_str": f"{random.randint(1, 59)}m",
        })
    return out


def _demo_log_lines(n: int = 35) -> list[str]:
    lines = []
    for i in range(n):
        t = datetime.now().strftime("%H:%M:%S")
        city = random.choice(_CITIES)
        choice = i % 6
        if choice == 0:
            lines.append(f"[dim]{t}[/]  [green]FILL #{i+1}[/] {city} YES {random.randint(10,50)}sh @ ${random.uniform(0.3,0.7):.4f}")
        elif choice == 1:
            lines.append(f"[dim]{t}[/]  [cyan]SCAN[/] {random.randint(80,140)} markets | {random.randint(1,6)} signals fired")
        elif choice == 2:
            a = round(random.uniform(44, 62), 1)
            b = round(a + random.uniform(-3, 3), 1)
            lines.append(f"[dim]{t}[/]  [blue]FCST[/] {city}: {a}F (NOAA) | {b}F (Open-Meteo) -> avg {(a+b)/2:.1f}F")
        elif choice == 3:
            pnl = random.uniform(-3, 8)
            col = "green" if pnl >= 0 else "red"
            sign = "+" if pnl >= 0 else "-"
            lines.append(f"[dim]{t}[/]  [bold {col}]CLOSE #{i+1}[/] {city} | PnL: {sign}${abs(pnl):.2f} | forecast shift")
        elif choice == 4:
            lines.append(f"[dim]{t}[/]  [yellow]NOAA[/] {city} target-date match: high {random.randint(44,58)}F")
        else:
            y = round(random.uniform(0.3, 0.6), 3)
            no = round(1 - y + random.uniform(-0.05, 0.05), 3)
            lines.append(f"[dim]{t}[/]  [magenta]ARBIT[/] Yes={y} No={no} edge={round(abs(y+no-1),3)}")
    return lines


def _demo_state() -> dict:
    positions = _demo_positions()
    invested = sum(p["cost"] for p in positions)
    upnl = round(sum(p["unrealized_pnl"] for p in positions), 2)
    balance = round(10000 + upnl - random.uniform(0, 20), 2)
    return {
        "balance": balance,
        "starting_balance": 10000.0,
        "total_pnl": round(upnl * 0.6, 2),
        "unrealized_pnl": upnl,
        "pnl_pct": round((balance - 10000) / 10000 * 100, 2),
        "total_trades": random.randint(12, 40),
        "win_rate": round(random.uniform(52, 78), 1),
        "open_positions": len(positions),
        "total_invested": round(invested, 2),
        "available": round(balance - invested, 2),
        "cycle": random.randint(50, 200),
        "dry_run": True,
        "positions": positions,
        "log_lines": _demo_log_lines(40),
        "pnl_history": [round(random.uniform(-8, 18), 2) for _ in range(50)],
    }


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _sparkline(history: list[float], width: int = 55) -> str:
    if not history:
        return "[dim]no data[/]"
    data = history[-width:]
    mn, mx = min(data), max(data)
    rng = mx - mn or 1
    chars = []
    for v in data:
        idx = int((v - mn) / rng * (len(SPARK_CHARS) - 1))
        col = "bright_green" if v >= 0 else "bright_red"
        chars.append(f"[{col}]{SPARK_CHARS[idx]}[/]")
    return "".join(chars)


def _conf_bar(conf: float, width: int = 8) -> Text:
    filled = round(conf * width)
    bar = "█" * filled + "░" * (width - filled)
    if conf >= 0.80:
        col = "bright_green"
    elif conf >= 0.65:
        col = "yellow"
    else:
        col = "bright_red"
    return Text(f"{bar} {conf:.0%}", style=col)


def _temp_marker(fc: float, lo: float, hi: float, unit: str) -> Text:
    mid = (lo + hi) / 2
    diff = fc - mid
    if abs(diff) < 1.0:
        return Text(f"{fc:.1f}{unit} ~MID", style="yellow")
    elif diff > 0:
        n = min(int(abs(diff) / 1.5), 4)
        return Text(f"{fc:.1f}{unit} {'▲'*max(n,1)} +{diff:.1f}", style="bright_red")
    else:
        n = min(int(abs(diff) / 1.5), 4)
        return Text(f"{fc:.1f}{unit} {'▼'*max(n,1)} {diff:.1f}", style="cyan")


def _sgn(v: float) -> str:
    return "+" if v >= 0 else ""


def _col(v: float) -> str:
    return "bright_green" if v >= 0 else "bright_red"


# ---------------------------------------------------------------------------
# WIDGETS
# ---------------------------------------------------------------------------

class StatBox(Static):
    DEFAULT_CSS = """
    StatBox {
        border: solid $primary-darken-2;
        background: $surface;
        padding: 0 1;
        height: 5;
        content-align: center middle;
    }
    StatBox:hover { border: solid $accent; background: $surface-lighten-1; }
    StatBox > .lbl { color: $text-muted; text-style: italic; }
    StatBox > .val { text-style: bold; }
    """

    def __init__(self, label: str, **kw):
        super().__init__(**kw)
        self._label = label

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="lbl")
        yield Label("--", classes="val")

    def set_val(self, text: str, style: str = "white"):
        try:
            self.query_one(".val", Label).update(Text(text, style=f"bold {style}"))
        except NoMatches:
            pass


class SparkPanel(Static):
    DEFAULT_CSS = """
    SparkPanel {
        height: 4;
        border: solid $primary-darken-2;
        background: $surface;
        padding: 0 2;
        width: 1fr;
    }
    """

    def set_history(self, history: list[float]):
        if not history:
            self.update("[dim]P&L HISTORY  no data yet[/]")
            return
        last = history[-1]
        spark = _sparkline(history)
        col = _col(last)
        self.update(
            f"[dim]P&L HISTORY[/]  [{col}]last: {_sgn(last)}${last:.2f}[/]\n{spark}"
        )


class PosTable(Static):
    DEFAULT_CSS = """
    PosTable {
        height: 100%;
        border: solid $primary-darken-2;
        background: $surface;
    }
    """
    COLS = [
        ("#",        3),
        ("City",    11),
        ("Question",32),
        ("Side",     4),
        ("Entry",    7),
        ("Now",      7),
        ("uPnL",    10),
        ("Conf",    14),
        ("Forecast",16),
        ("Age",      5),
    ]

    def compose(self) -> ComposeResult:
        yield Label("[bold cyan] OPEN POSITIONS[/]", markup=True)
        yield DataTable(zebra_stripes=True, cursor_type="row", id="dt")

    def on_mount(self):
        dt = self.query_one("#dt", DataTable)
        for name, width in self.COLS:
            dt.add_column(name, width=width)

    def refresh_data(self, positions: list[dict]):
        dt = self.query_one("#dt", DataTable)
        dt.clear()
        if not positions:
            dt.add_row(Text("no open positions", style="dim"), *[""] * (len(self.COLS) - 1))
            return
        for p in positions:
            entry = p.get("entry_price", 0)
            cur = p.get("current_price", entry)
            upnl = p.get("unrealized_pnl", 0)
            conf = p.get("confidence", 0)
            fc   = p.get("forecast_temp", 0)
            lo   = p.get("temp_range_low", 0)
            hi   = p.get("temp_range_high", 0)
            unit = p.get("forecast_unit", "F")
            outcome = p.get("outcome", "?")
            q = p.get("question", "")
            q_short = (q[:30] + "..") if len(q) > 32 else q

            pnl_col = _col(upnl)
            cur_col = "bright_green" if cur >= entry else "bright_red"
            side_col = "bold cyan" if outcome == "Yes" else "bold magenta"

            dt.add_row(
                Text(str(p.get("id", "?")), style="dim white"),
                Text(p.get("city", "?"), style="bold white"),
                Text(q_short, style="dim white"),
                Text(outcome, style=side_col),
                Text(f"${entry:.3f}", style="dim white"),
                Text(f"${cur:.3f}", style=f"bold {cur_col}"),
                Text(f"{_sgn(upnl)}${upnl:.2f}", style=f"bold {pnl_col}"),
                _conf_bar(conf, 8),
                _temp_marker(fc, lo, hi, unit),
                Text(p.get("age_str", "?"), style="dim"),
            )


class LogPanel(Static):
    DEFAULT_CSS = """
    LogPanel {
        height: 100%;
        border: solid $primary-darken-2;
        background: $surface;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("[bold cyan] ACTIVITY LOG[/]", markup=True)
        yield Log(id="lg", highlight=True, max_lines=300)

    def push(self, line: str):
        try:
            ts = datetime.now().strftime("%H:%M:%S")
            self.query_one("#lg", Log).write_line(f"[dim]{ts}[/]  {line}")
        except NoMatches:
            pass

    def clear_log(self):
        try:
            self.query_one("#lg", Log).clear()
        except NoMatches:
            pass


# ---------------------------------------------------------------------------
# STYLESHEET
# ---------------------------------------------------------------------------
APP_CSS = """
Screen { background: $background; }

#topbar {
    height: 1;
    background: $primary-darken-3;
    layout: horizontal;
}
#topbar Label { padding: 0 2; }
#lbl_title  { color: ansi_bright_green; text-style: bold; }
#lbl_mode   { color: yellow; text-style: bold; }
#lbl_cycle  { color: $text-muted; }
#lbl_time   { color: $text-muted; dock: right; }

#stat_row {
    height: 5;
    layout: horizontal;
}
StatBox { width: 1fr; margin: 0 1; }

#spark_row { height: 4; margin: 0 1; }

#body {
    layout: horizontal;
    height: 1fr;
    margin: 0 1;
}
#left_col  { width: 3fr; margin-right: 1; }
#right_col { width: 2fr; }

PosTable   { height: 1fr; margin-bottom: 1; }
LogPanel   { height: 1fr; }

Footer { background: $primary-darken-3; color: $text-muted; }
"""


# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------
class BotDashboard(App):
    TITLE = "POLYMARKET WEATHER BOT"
    SUB_TITLE = "live edge detection"
    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "quit",       "Quit",         priority=True),
        Binding("r", "refresh",    "Refresh"),
        Binding("c", "clear_log",  "Clear Log"),
        Binding("p", "pause",      "Pause/Resume"),
        Binding("?", "show_help",  "Help"),
    ]

    paused: reactive[bool] = reactive(False)

    def __init__(self, demo: bool = False):
        super().__init__()
        self._demo = demo
        self._log_cursor = 0
        self._pnl_history: list[float] = []

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label("◉ POLYMARKET WEATHER BOT", id="lbl_title")
            yield Label("PAPER", id="lbl_mode")
            yield Label("cycle 0", id="lbl_cycle")
            yield Label("", id="lbl_time")

        with Horizontal(id="stat_row"):
            yield StatBox("BALANCE",      id="sb_bal")
            yield StatBox("REALIZED P&L", id="sb_rpnl")
            yield StatBox("UNREALIZED",   id="sb_upnl")
            yield StatBox("WIN RATE",     id="sb_wr")
            yield StatBox("POSITIONS",    id="sb_pos")
            yield StatBox("INVESTED",     id="sb_inv")
            yield StatBox("AVAILABLE",    id="sb_avail")

        with Horizontal(id="spark_row"):
            yield SparkPanel(id="spark")

        with Horizontal(id="body"):
            with Vertical(id="left_col"):
                yield PosTable(id="pos_panel")
            with Vertical(id="right_col"):
                yield LogPanel(id="log_panel")

        yield Footer()

    # -- lifecycle -----------------------------------------------------------

    def on_mount(self):
        self.set_interval(3, self._tick)
        self._tick()
        if self._demo:
            lp = self.query_one("#log_panel", LogPanel)
            lp.push("[bold green]DEMO MODE[/] - fake data, refreshes every 3s")
            lp.push("[dim]Start bot and remove --demo to go live[/]")

    # -- actions -------------------------------------------------------------

    def action_quit(self):      self.exit()
    def action_refresh(self):
        self._tick()
        self.query_one("#log_panel", LogPanel).push("[cyan]Manual refresh[/]")
    def action_clear_log(self): self.query_one("#log_panel", LogPanel).clear_log()
    def action_pause(self):
        self.paused = not self.paused
        label = "[yellow]PAUSED[/]" if self.paused else "[green]LIVE[/]"
        self.query_one("#log_panel", LogPanel).push(f"Updates {label}")
    def action_show_help(self):
        self.query_one("#log_panel", LogPanel).push(
            "[bold cyan]KEYS:[/]  R=refresh  C=clear  P=pause  Q=quit"
        )

    # -- tick ----------------------------------------------------------------

    def _tick(self):
        if self.paused:
            return
        state = _demo_state() if self._demo else self._read_state()
        if state:
            self._apply(state)

    def _read_state(self) -> dict:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
        return {}

    def _apply(self, s: dict):
        # top bar
        ts = datetime.now().strftime("%H:%M:%S")
        self.query_one("#lbl_time",  Label).update(f"{ts}  ")
        self.query_one("#lbl_cycle", Label).update(f"cycle {s.get('cycle', 0)}")
        mode_txt = "[bold yellow]PAPER[/]" if s.get("dry_run", True) else "[bold bright_red]LIVE[/]"
        self.query_one("#lbl_mode",  Label).update(mode_txt)

        # stats
        bal   = s.get("balance",        0.0)
        rpnl  = s.get("total_pnl",      0.0)
        upnl  = s.get("unrealized_pnl", 0.0)
        winr  = s.get("win_rate",        0.0)
        npos  = s.get("open_positions",    0)
        inv   = s.get("total_invested",  0.0)
        avail = s.get("available",       0.0)
        pct   = s.get("pnl_pct",         0.0)

        self.query_one("#sb_bal",   StatBox).set_val(f"${bal:,.2f}")
        self.query_one("#sb_rpnl",  StatBox).set_val(f"{_sgn(rpnl)}${rpnl:.2f} ({_sgn(pct)}{pct:.2f}%)", _col(rpnl))
        self.query_one("#sb_upnl",  StatBox).set_val(f"{_sgn(upnl)}${upnl:.2f}", _col(upnl))
        self.query_one("#sb_wr",    StatBox).set_val(f"{winr:.1f}%", "bright_green" if winr >= 55 else "yellow")
        self.query_one("#sb_pos",   StatBox).set_val(f"{npos} open", "cyan")
        self.query_one("#sb_inv",   StatBox).set_val(f"${inv:.2f}")
        self.query_one("#sb_avail", StatBox).set_val(f"${avail:.2f}", "bright_green" if avail > 100 else "yellow")

        # sparkline
        hist = list(s.get("pnl_history", []))
        if upnl != 0:
            hist.append(upnl)
        self._pnl_history = hist
        self.query_one("#spark", SparkPanel).set_history(hist)

        # positions table
        self.query_one("#pos_panel", PosTable).refresh_data(s.get("positions", []))

        # log - only new lines
        log_lines = s.get("log_lines", [])
        lp = self.query_one("#log_panel", LogPanel)
        new_lines = log_lines[self._log_cursor:]
        for line in new_lines:
            lp.push(line)
        self._log_cursor = len(log_lines)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Polymarket Bot Dashboard")
    parser.add_argument("--demo", action="store_true", help="Run with fake demo data")
    args = parser.parse_args()
    demo = args.demo or not STATE_FILE.exists()
    BotDashboard(demo=demo).run()


if __name__ == "__main__":
    main()
