"""Function selectors and output layouts for the read-only calls this app makes.

We talk raw JSON-RPC (see ``rpc.py``) rather than going through web3 contract
objects, so all we need per function is its 4-byte selector and the ABI types
of its inputs and outputs.
"""
from __future__ import annotations

from eth_utils import keccak


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


# name -> (signature, input types, output types)
FUNCTIONS: dict[str, tuple[str, list[str], list[str]]] = {
    "getPool": ("getPool(address,address,uint24)", ["address", "address", "uint24"], ["address"]),
    "slot0": ("slot0()", [], ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]),
    "token0": ("token0()", [], ["address"]),
    "token1": ("token1()", [], ["address"]),
    "fee": ("fee()", [], ["uint24"]),
    "tickSpacing": ("tickSpacing()", [], ["int24"]),
    "liquidity": ("liquidity()", [], ["uint128"]),
    "feeGrowthGlobal0X128": ("feeGrowthGlobal0X128()", [], ["uint256"]),
    "feeGrowthGlobal1X128": ("feeGrowthGlobal1X128()", [], ["uint256"]),
    "ticks": ("ticks(int24)", ["int24"],
              ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]),
}

SELECTORS = {name: selector(sig) for name, (sig, _, _) in FUNCTIONS.items()}

# event Swap(address indexed sender, address indexed recipient, int256 amount0, int256 amount1,
#            uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
SWAP_TOPIC = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
SWAP_DATA_TYPES = ["int256", "int256", "uint160", "uint128", "int24"]
