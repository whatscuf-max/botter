"""
Polymarket Weather Trading Bot - Configuration
"""

import os
from dataclasses import dataclass, field


@dataclass
class WalletConfig:
    private_key: str = ""
    funder_address: str = ""
    signature_type: int = 0


@dataclass
class APIConfig:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137


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
    wallet: WalletConfig = field(default_factory=WalletConfig)
    api: APIConfig = field(default_factory=APIConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    logging: LogConfig = field(default_factory=LogConfig)
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> "BotConfig":
        c = cls()
        c.wallet.private_key = os.getenv("POLYMARKET_PK", "")
        c.wallet.funder_address = os.getenv("POLYMARKET_FUNDER", "")
        c.wallet.signature_type = int(os.getenv("POLYMARKET_SIG_TYPE", "0"))
        c.trading.starting_balance = float(os.getenv("STARTING_BALANCE", "30"))
        c.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        return c
