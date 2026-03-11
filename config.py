# Kalshi Weather Trading Bot - Configuration

import os
from dataclasses import dataclass, field


@dataclass
class KalshiConfig:
    api_key_id: str = ""
    private_key_path: str = ""
    base_url: str = "https://trading-api.kalshi.com/trade-api/v2"
    demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    use_demo: bool = False

    @property
    def active_url(self) -> str:
        return self.demo_url if self.use_demo else self.base_url


@dataclass
class TradingConfig:
    starting_balance: float = 30.0
    max_position_pct: float = 0.06
    max_daily_loss_pct: float = 0.15
    max_concurrent_positions: int = 250
    min_arb_spread: float = 0.015
    arb_fee_rate: float = 0.02
    arb_min_liquidity: float = 10.0
    compound_profits: bool = True
    scan_interval: int = 10
    momentum_threshold: float = 0.55


@dataclass
class LogConfig:
    log_file: str = "trading_log.json"
    log_level: str = "INFO"


@dataclass
class BotConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    logging: LogConfig = field(default_factory=LogConfig)
    dry_run: bool = True

    @classmethod
    def from_env(cls):
        c = cls()
        c.kalshi.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        c.kalshi.private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
        c.kalshi.use_demo = os.getenv("KALSHI_USE_DEMO", "false").lower() == "true"
        c.trading.starting_balance = float(os.getenv("STARTING_BALANCE", "30"))
        c.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        return c


# Kalshi temperature series -> NWS station + exact coordinates
# Settlement: NWS Daily Climatological Report released the following morning
# IMPORTANT: Kalshi prices are in CENTS (int 1-99), not decimals (0.01-0.99)
KALSHI_WEATHER_SERIES = {
    "KXHIGHNY":   {"city": "New York City",  "station": "KNYC", "lat": 40.7789,  "lon": -73.9692},
    "KXHIGHCHI":  {"city": "Chicago",        "station": "KMDW", "lat": 41.7868,  "lon": -87.7522},
    "KXHIGHMIA":  {"city": "Miami",          "station": "KMIA", "lat": 25.7959,  "lon": -80.2870},
    "KXHIGHDEN":  {"city": "Denver",         "station": "KDEN", "lat": 39.8561,  "lon": -104.6737},
    "KXHIGHHOU":  {"city": "Houston",        "station": "KHOU", "lat": 29.6454,  "lon": -95.2789},
    "KXHIGHPHL":  {"city": "Philadelphia",   "station": "KPHL", "lat": 39.8719,  "lon": -75.2411},
    "KXHIGHATL":  {"city": "Atlanta",        "station": "KATL", "lat": 33.6407,  "lon": -84.4277},
    "KXHIGHBOS":  {"city": "Boston",         "station": "KBOS", "lat": 42.3656,  "lon": -71.0096},
    "KXHIGHLAX":  {"city": "Los Angeles",    "station": "KLAX", "lat": 33.9425,  "lon": -118.4081},
    "KXHIGHDFW":  {"city": "Dallas",         "station": "KDFW", "lat": 32.8998,  "lon": -97.0403},
    "KXHIGHLAS":  {"city": "Las Vegas",      "station": "KLAS", "lat": 36.0840,  "lon": -115.1537},
    "KXHIGHPHX":  {"city": "Phoenix",        "station": "KPHX", "lat": 33.4373,  "lon": -112.0078},
    "KXHIGHSFO":  {"city": "San Francisco",  "station": "KSFO", "lat": 37.6213,  "lon": -122.3790},
    "KXHIGHSEA":  {"city": "Seattle",        "station": "KSEA", "lat": 47.4502,  "lon": -122.3088},
    "KXHIGHDC":   {"city": "Washington DC",  "station": "KDCA", "lat": 38.8521,  "lon": -77.0377},
    "KXHIGHMSP":  {"city": "Minneapolis",    "station": "KMSP", "lat": 44.8848,  "lon": -93.2223},
    "KXHIGHOKC":  {"city": "Oklahoma City",  "station": "KOKC", "lat": 35.3931,  "lon": -97.6008},
    "KXHIGHSAT":  {"city": "San Antonio",    "station": "KSAT", "lat": 29.5337,  "lon": -98.4698},
    "KXHIGHAUST": {"city": "Austin",         "station": "KAUS", "lat": 30.1975,  "lon": -97.6664},
}

STATION_TO_SERIES = {v["station"]: k for k, v in KALSHI_WEATHER_SERIES.items()}
