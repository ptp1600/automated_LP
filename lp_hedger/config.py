"""Persistent user configuration.

Everything the user sets through the UI lives in one JSON file under the data
directory (default ``./data``). Paper trading needs no secrets: there is no
wallet, no API key and nothing is ever signed.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    d = Path(os.environ.get("LP_HEDGER_DATA", "data")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class ChainSettings:
    chain: str = "base"                # key into chains.CHAINS (ethereum | arbitrum | base)
    rpc_url: str = ""                  # empty -> chain preset public RPCs (with failover)
    pool: str = "ETH/USDC 0.05%"       # key into chain preset pools


@dataclass
class LPSettings:
    deploy_usd: float = 10000.0        # paper position size in USD
    range_down_pct: float = 10.0       # lower bound = price * (1 - range_down_pct/100)
    range_up_pct: float = 10.0         # upper bound = price * (1 + range_up_pct/100)
    auto_rebalance: bool = False       # re-center (paper) when out of range
    rebalance_after_min: int = 60      # price must be out of range for this long
    simulate_entry_costs: bool = True  # charge swap fee + price impact + gas on open/close


@dataclass
class DeriveSettings:
    api_version: str = "v2"            # v2 (live on mainnet today) | v3 (Ethereum L1; testnet now)
    environment: str = "mainnet"       # mainnet | testnet  (public market data only, no account needed)
    currency: str = "ETH"


@dataclass
class HedgeSettings:
    enabled: bool = True
    mode: str = "protective_put"       # protective_put | delta_neutral
    coverage_pct: float = 100.0        # % of downside exposure to cover
    strike_offset_pct: float = 0.0     # strike = range_low * (1 + offset/100)
    target_days_to_expiry: int = 14    # prefer expiries near this
    min_days_to_expiry: int = 3        # never buy anything closer than this
    roll_days_before_expiry: int = 2   # roll when held hedge is this close
    max_premium_pct: float = 3.0       # per hedge purchase, % of LP value (safety cap)
    rehedge_tolerance_pct: float = 15.0  # ignore drifts smaller than this of target
    allow_reduce: bool = True          # sell puts when over-hedged
    slippage_pct: float = 3.0          # paper fills: price for size beyond the top of book
    strike_drift_pct: float = 15.0     # keep existing hedge if strike within this of target


@dataclass
class EngineSettings:
    poll_interval_sec: int = 60
    autostart: bool = True             # start polling when the app launches
    history_days: int = 5              # volume / fee lookback for projections
    projection_days: int = 14          # horizon for the "what would I make" estimate


@dataclass
class Config:
    chain: ChainSettings = field(default_factory=ChainSettings)
    lp: LPSettings = field(default_factory=LPSettings)
    derive: DeriveSettings = field(default_factory=DeriveSettings)
    hedge: HedgeSettings = field(default_factory=HedgeSettings)
    engine: EngineSettings = field(default_factory=EngineSettings)

    # ---- persistence -----------------------------------------------------
    @staticmethod
    def path() -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if not p.exists():
            return cls()
        with p.open() as f:
            raw = json.load(f)
        return cls.from_dict(raw)

    def save(self) -> None:
        tmp = self.path().with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(self.to_dict(), f, indent=2)
        tmp.replace(self.path())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        cfg = cls()
        for section in fields(cls):
            sub = getattr(cfg, section.name)
            for k, v in (raw.get(section.name) or {}).items():
                if hasattr(sub, k):
                    setattr(sub, k, _coerce(getattr(sub, k), v))
        return cfg

    def update(self, patch: dict[str, Any]) -> None:
        """Apply a partial nested dict (as sent by the UI)."""
        merged = self.to_dict()
        for section, values in patch.items():
            if section in merged and isinstance(values, dict):
                merged[section].update(values)
        new = self.from_dict(merged)
        for section in fields(self):
            setattr(self, section.name, getattr(new, section.name))


def _coerce(current: Any, value: Any) -> Any:
    """Coerce UI strings to the type of the default value."""
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    return value
