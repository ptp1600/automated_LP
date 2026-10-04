"""Minimal ABIs: only the functions this app calls."""

ERC20 = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "string"}]},
]

WETH = ERC20 + [
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]

FACTORY = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}, {"name": "fee", "type": "uint24"}],
     "outputs": [{"name": "", "type": "address"}]},
]

POOL = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint24"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "int24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint128"}]},
]

_MINT_PARAMS = [
    {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
    {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
    {"name": "tickUpper", "type": "int24"}, {"name": "amount0Desired", "type": "uint256"},
    {"name": "amount1Desired", "type": "uint256"}, {"name": "amount0Min", "type": "uint256"},
    {"name": "amount1Min", "type": "uint256"}, {"name": "recipient", "type": "address"},
    {"name": "deadline", "type": "uint256"},
]

POSITION_MANAGER = [
    {"name": "mint", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": _MINT_PARAMS}],
     "outputs": [{"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
                 {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
         {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
         {"name": "deadline", "type": "uint256"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
         {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "burn", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": []},
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [
         {"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
         {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
         {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"},
         {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"},
         {"name": "feeGrowthInside0LastX128", "type": "uint256"},
         {"name": "feeGrowthInside1LastX128", "type": "uint256"},
         {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "tokenOfOwnerByIndex", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "index", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
]

# Uniswap SwapRouter02 (no deadline field in the params struct)
SWAP_ROUTER02 = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
         {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]
