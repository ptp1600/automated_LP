"""Read-only view of a Uniswap v3 pool over JSON-RPC.

Everything the paper trader learns about a pool comes from here: price and
tick, active liquidity, the global fee-growth accumulators (fees per unit of
liquidity since inception), per-tick fee-growth data, gas price, and raw
``Swap`` events for the history backfill. Nothing is ever signed or sent.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from eth_abi.abi import decode
from eth_utils import to_checksum_address

from . import abis
from .chains import TICK_SPACING_BY_FEE, ChainPreset, PoolPreset
from .rpc import ArchiveUnsupported, JsonRpc, LogRangeTooWide, RpcError
from .uniswap_math import PoolMeta

Q128 = 2**128
U256 = 2**256


@dataclass
class PoolState:
    block: int
    ts: float
    sqrt_price_x96: int
    tick: int
    price: float            # USD per ETH
    liquidity: int          # active liquidity at the current tick
    fg0: int                # feeGrowthGlobal0X128
    fg1: int                # feeGrowthGlobal1X128

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PoolState":
        return PoolState(**{k: d[k] for k in PoolState.__dataclass_fields__})


@dataclass
class TickInfo:
    fee_growth_outside0: int
    fee_growth_outside1: int
    initialized: bool


@dataclass
class Swap:
    block: int
    amount0: int            # positive = pool received token0
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int


def fee_growth_inside(tick_lower: int, tick_upper: int, tick_current: int, fg0: int, fg1: int,
                      lower: TickInfo, upper: TickInfo) -> tuple[int, int]:
    """Uniswap's Tick.getFeeGrowthInside, modulo 2^256 like the contract."""
    if tick_current >= tick_lower:
        below0, below1 = lower.fee_growth_outside0, lower.fee_growth_outside1
    else:
        below0, below1 = (fg0 - lower.fee_growth_outside0) % U256, (fg1 - lower.fee_growth_outside1) % U256
    if tick_current < tick_upper:
        above0, above1 = upper.fee_growth_outside0, upper.fee_growth_outside1
    else:
        above0, above1 = (fg0 - upper.fee_growth_outside0) % U256, (fg1 - upper.fee_growth_outside1) % U256
    return (fg0 - below0 - above0) % U256, (fg1 - below1 - above1) % U256


class PoolReader:
    def __init__(self, chain: ChainPreset, pool: PoolPreset, rpc_url: str = "",
                 log: Optional[Callable[[str, str], None]] = None, timeout: float = 30):
        self.chain = chain
        self.pool_preset = pool
        self.log = log or (lambda level, msg: None)
        self.timeout = timeout
        self.candidates = [rpc_url] if rpc_url else list(chain.rpcs)
        self.rpc = JsonRpc(self.candidates[0], timeout=timeout)
        self.address: Optional[str] = None
        self.meta: Optional[PoolMeta] = None
        self.token0: Optional[str] = None
        self.token1: Optional[str] = None

    # ---- rpc management ------------------------------------------------------
    @property
    def rpc_url(self) -> str:
        return self.rpc.url

    def rotate_rpc(self) -> Optional[str]:
        if len(self.candidates) < 2:
            return None
        i = self.candidates.index(self.rpc.url) if self.rpc.url in self.candidates else -1
        nxt = self.candidates[(i + 1) % len(self.candidates)]
        self.rpc = JsonRpc(nxt, timeout=self.timeout)
        return nxt

    def connected(self) -> bool:
        try:
            return self.rpc.chain_id() == self.chain.chain_id
        except RpcError:
            return False

    # ---- metadata ------------------------------------------------------------
    def resolve(self) -> str:
        """Find the pool through the factory and learn its token order."""
        if self.address:
            return self.address
        c = self.chain
        (addr,) = self.rpc.eth_call(c.factory, "getPool", [to_checksum_address(c.weth), to_checksum_address(c.usdc),
                                                            self.pool_preset.fee])
        if int(addr, 16) == 0:
            raise ValueError(f"No {self.pool_preset.name} pool on {c.name}")
        addr = to_checksum_address(addr)
        (t0,), (t1,), (fee,), (spacing,) = self.rpc.eth_calls(addr, [("token0", []), ("token1", []), ("fee", []),
                                                                     ("tickSpacing", [])])
        eth_is_token0 = t0.lower() == c.weth.lower()
        if not eth_is_token0 and t1.lower() != c.weth.lower():
            raise ValueError("Pool does not contain WETH")
        self.token0, self.token1 = to_checksum_address(t0), to_checksum_address(t1)
        self.meta = PoolMeta(eth_is_token0=eth_is_token0, eth_decimals=18, usd_decimals=c.usdc_decimals,
                             tick_spacing=TICK_SPACING_BY_FEE.get(fee) or spacing)
        self.address = addr
        return addr

    # ---- state -----------------------------------------------------------------
    def state(self, block: "int | str" = "latest", rpc: Optional[JsonRpc] = None) -> PoolState:
        """Price, liquidity and fee growth at one block (historical blocks need an archive node)."""
        rpc = rpc or self.rpc
        addr = self.resolve()
        assert self.meta is not None
        slot0, (liq,), (fg0,), (fg1,) = rpc.eth_calls(addr, [("slot0", []), ("liquidity", []),
                                                             ("feeGrowthGlobal0X128", []),
                                                             ("feeGrowthGlobal1X128", [])], block)
        blk = rpc.get_block(block)
        return PoolState(block=blk["number"], ts=float(blk["timestamp"]), sqrt_price_x96=slot0[0], tick=slot0[1],
                         price=self.meta.price_from_sqrt_x96(slot0[0]), liquidity=liq, fg0=fg0, fg1=fg1)

    def tick_infos(self, ticks: list[int], block: "int | str" = "latest") -> list[TickInfo]:
        addr = self.resolve()
        rows = self.rpc.eth_calls(addr, [("ticks", [t]) for t in ticks], block)
        return [TickInfo(fee_growth_outside0=r[2], fee_growth_outside1=r[3], initialized=bool(r[7])) for r in rows]

    def fee_growth_inside(self, tick_lower: int, tick_upper: int, st: PoolState) -> Optional[tuple[int, int]]:
        """Exact fee growth inside a range, or None when a boundary tick is not
        initialized on-chain (the contract then has no data for it)."""
        try:
            lower, upper = self.tick_infos([tick_lower, tick_upper], st.block)
        except (ArchiveUnsupported, RpcError):
            try:
                lower, upper = self.tick_infos([tick_lower, tick_upper])
            except RpcError:
                return None
        if not (lower.initialized and upper.initialized):
            return None
        return fee_growth_inside(tick_lower, tick_upper, st.tick, st.fg0, st.fg1, lower, upper)

    def gas_price_wei(self) -> int:
        try:
            return self.rpc.gas_price()
        except RpcError:
            return 0

    # ---- swap logs ---------------------------------------------------------------
    def swaps(self, from_block: int, to_block: int, rpc: Optional[JsonRpc] = None) -> list[Swap]:
        rpc = rpc or self.rpc
        addr = self.resolve()
        out = []
        for lg in rpc.get_logs(addr, [abis.SWAP_TOPIC], from_block, to_block):
            a0, a1, sp, liq, tick = decode(abis.SWAP_DATA_TYPES, bytes.fromhex(lg["data"][2:]))
            out.append(Swap(block=int(lg["blockNumber"], 16), amount0=a0, amount1=a1, sqrt_price_x96=sp,
                            liquidity=liq, tick=tick))
        return out


__all__ = ["PoolReader", "PoolState", "Swap", "TickInfo", "fee_growth_inside", "Q128", "U256",
           "ArchiveUnsupported", "LogRangeTooWide", "RpcError"]
