"""Chain and pool presets for Uniswap v3 deployments (read-only paper trading).

Addresses are the canonical Uniswap Labs deployments. Which token is token0
is derived from the pool contract at runtime, so the order here is only
informational.

``rpcs`` lists public endpoints in preference order. Free public nodes differ
in what they serve: some answer ``eth_call`` at historical blocks ("archive"),
some serve ``eth_getLogs`` far back, some neither. The history backfill tries
every listed endpoint with both methods, so the more the better.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PoolPreset:
    """A WETH/USDC pool identified by fee tier; the address is resolved from the
    Uniswap factory at runtime so nothing here can go stale."""

    name: str
    fee: int                    # in hundredths of a bip (500 = 0.05%)

    @property
    def fee_rate(self) -> float:
        return self.fee / 1_000_000


@dataclass(frozen=True)
class ChainPreset:
    key: str
    name: str
    chain_id: int
    explorer: str
    weth: str
    usdc: str
    usdc_decimals: int
    factory: str
    rpcs: tuple[str, ...]              # first = default for live polling
    block_time_sec: float              # rough; used to plan the history backfill
    logs_max_blocks: int               # largest eth_getLogs range public nodes accept
    gas_open_units: int                # approve + mint (simulated entry cost)
    gas_close_units: int               # decrease + collect + burn
    pools: dict[str, PoolPreset] = field(default_factory=dict)

    @property
    def default_rpc(self) -> str:
        return self.rpcs[0]


_POOLS = {
    "ETH/USDC 0.05%": PoolPreset("ETH/USDC 0.05%", 500),
    "ETH/USDC 0.3%": PoolPreset("ETH/USDC 0.3%", 3000),
}

CHAINS: dict[str, ChainPreset] = {
    "ethereum": ChainPreset(
        key="ethereum",
        name="Ethereum",
        chain_id=1,
        explorer="https://etherscan.io",
        weth="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        usdc="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        usdc_decimals=6,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=("https://ethereum-rpc.publicnode.com", "https://rpc.mevblocker.io", "https://eth.drpc.org",
              "https://eth-mainnet.public.blastapi.io", "https://0xrpc.io/eth"),
        block_time_sec=12.0,
        logs_max_blocks=2000,
        gas_open_units=550_000,
        gas_close_units=350_000,
        pools=dict(_POOLS),
    ),
    "arbitrum": ChainPreset(
        key="arbitrum",
        name="Arbitrum One",
        chain_id=42161,
        explorer="https://arbiscan.io",
        weth="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        usdc_decimals=6,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        rpcs=("https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com", "https://arbitrum.drpc.org"),
        block_time_sec=0.25,
        logs_max_blocks=2000,
        gas_open_units=1_200_000,
        gas_close_units=800_000,
        pools=dict(_POOLS),
    ),
    "base": ChainPreset(
        key="base",
        name="Base",
        chain_id=8453,
        explorer="https://basescan.org",
        weth="0x4200000000000000000000000000000000000006",
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        usdc_decimals=6,
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        rpcs=("https://base-rpc.publicnode.com", "https://mainnet.base.org", "https://base.drpc.org"),
        block_time_sec=2.0,
        logs_max_blocks=2000,
        gas_open_units=550_000,
        gas_close_units=350_000,
        pools=dict(_POOLS),
    ),
}

TICK_SPACING_BY_FEE = {100: 1, 500: 10, 3000: 60, 10000: 200}


def get_chain(key: str) -> ChainPreset:
    try:
        return CHAINS[key]
    except KeyError:
        raise ValueError(f"Unknown chain '{key}'. Options: {', '.join(CHAINS)}")


def get_pool(chain: ChainPreset, name: str) -> PoolPreset:
    return chain.pools.get(name) or next(iter(chain.pools.values()))


def presets_for_ui() -> list[dict]:
    return [
        {
            "key": c.key,
            "name": c.name,
            "chain_id": c.chain_id,
            "default_rpc": c.default_rpc,
            "explorer": c.explorer,
            "pools": [{"name": p.name, "fee": p.fee} for p in c.pools.values()],
        }
        for c in CHAINS.values()
    ]
