"""Chain and pool presets for Uniswap v3 deployments.

Addresses are the canonical Uniswap Labs deployments. ``eth_is_token0`` is
derived at runtime from the pool contract, so the order here is informational.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PoolPreset:
    """A WETH/USDC pool identified by fee tier; the address is resolved from the
    Uniswap factory at runtime so nothing here can go stale."""

    name: str
    fee: int                    # in hundredths of a bip (500 = 0.05%)


@dataclass(frozen=True)
class ChainPreset:
    key: str
    name: str
    chain_id: int
    default_rpc: str
    explorer: str
    weth: str
    usdc: str
    usdc_decimals: int
    factory: str
    position_manager: str
    swap_router02: str
    pools: dict[str, PoolPreset] = field(default_factory=dict)


CHAINS: dict[str, ChainPreset] = {
    "arbitrum": ChainPreset(
        key="arbitrum",
        name="Arbitrum One",
        chain_id=42161,
        default_rpc="https://arb1.arbitrum.io/rpc",
        explorer="https://arbiscan.io",
        weth="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        usdc_decimals=6,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        position_manager="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        swap_router02="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        pools={
            "ETH/USDC 0.05%": PoolPreset("ETH/USDC 0.05%", 500),
            "ETH/USDC 0.3%": PoolPreset("ETH/USDC 0.3%", 3000),
        },
    ),
    "base": ChainPreset(
        key="base",
        name="Base",
        chain_id=8453,
        default_rpc="https://mainnet.base.org",
        explorer="https://basescan.org",
        weth="0x4200000000000000000000000000000000000006",
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        usdc_decimals=6,
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        position_manager="0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        swap_router02="0x2626664c2603336E57B271c5C0b26F421741e481",
        pools={
            "ETH/USDC 0.05%": PoolPreset("ETH/USDC 0.05%", 500),
            "ETH/USDC 0.3%": PoolPreset("ETH/USDC 0.3%", 3000),
        },
    ),
}

TICK_SPACING_BY_FEE = {100: 1, 500: 10, 3000: 60, 10000: 200}


def get_chain(key: str) -> ChainPreset:
    try:
        return CHAINS[key]
    except KeyError:
        raise ValueError(f"Unknown chain '{key}'. Options: {', '.join(CHAINS)}")


def presets_for_ui() -> list[dict]:
    return [
        {
            "key": c.key,
            "name": c.name,
            "chain_id": c.chain_id,
            "default_rpc": c.default_rpc,
            "pools": [{"name": p.name, "fee": p.fee} for p in c.pools.values()],
        }
        for c in CHAINS.values()
    ]
