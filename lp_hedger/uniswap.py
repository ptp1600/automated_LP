"""On-chain Uniswap v3 operations through web3.py.

All write operations go through ``_send`` which respects ``dry_run``: in dry
run the transaction is built and gas-estimated (so reverts surface) but never
broadcast.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

import requests
from web3 import Web3
from web3.contract import Contract
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from . import abis
from .chains import TICK_SPACING_BY_FEE, ChainPreset, PoolPreset
from .uniswap_math import (
    MAX_UINT128,
    LPPosition,
    PoolMeta,
    plan_mint,
    sqrt_ratio_at_tick,
    amounts_for_liquidity,
)
from .wallet import HotWallet

MAX_UINT256 = 2**256 - 1


@dataclass
class TxResult:
    tx_hash: Optional[str]
    dry_run: bool
    gas: int
    description: str
    receipt: Optional[dict] = None


class UniswapClient:
    def __init__(self, chain: ChainPreset, pool: PoolPreset, rpc_url: str, wallet: HotWallet,
                 dry_run: bool = True, slippage_pct: float = 0.5,
                 log: Optional[Callable[[str, str], None]] = None):
        self.chain = chain
        self.pool_preset = pool
        self.wallet = wallet
        self.dry_run = dry_run
        self.slippage = slippage_pct / 100.0
        self.log = log or (lambda level, msg: None)
        self._user_rpc = rpc_url
        self._candidates = [rpc_url] if rpc_url else [chain.default_rpc, *chain.fallback_rpcs]
        self._pool_address: Optional[str] = None
        self._meta: Optional[PoolMeta] = None
        self._token0: Optional[str] = None
        self._token1: Optional[str] = None
        self.rpc_url = self._pick_rpc(rpc_url)
        self._bind(self.rpc_url)

    def _bind(self, url: str) -> None:
        """(Re)create the web3 instance and contract handles for an RPC URL."""
        self.rpc_url = url
        self.w3 = self._make_w3(url)
        c = self.chain
        self.factory: Contract = self.w3.eth.contract(Web3.to_checksum_address(c.factory), abi=abis.FACTORY)
        self.npm: Contract = self.w3.eth.contract(Web3.to_checksum_address(c.position_manager), abi=abis.POSITION_MANAGER)
        self.router: Contract = self.w3.eth.contract(Web3.to_checksum_address(c.swap_router02), abi=abis.SWAP_ROUTER02)
        self.weth: Contract = self.w3.eth.contract(Web3.to_checksum_address(c.weth), abi=abis.WETH)
        self.usdc: Contract = self.w3.eth.contract(Web3.to_checksum_address(c.usdc), abi=abis.ERC20)
        self._pool: Optional[Contract] = None
        if self._pool_address:
            self._pool = self.w3.eth.contract(Web3.to_checksum_address(self._pool_address), abi=abis.POOL)

    def rotate_rpc(self) -> Optional[str]:
        """Switch to the next preset RPC (after a 429 etc). Returns the new URL or None."""
        if len(self._candidates) < 2:
            return None
        i = self._candidates.index(self.rpc_url) if self.rpc_url in self._candidates else -1
        nxt = self._candidates[(i + 1) % len(self._candidates)]
        self._bind(nxt)
        return nxt

    # ---- rpc -------------------------------------------------------------------
    @staticmethod
    def _make_w3(url: str) -> Web3:
        # Public RPCs rate-limit (HTTP 429): retry reads with exponential backoff.
        retry = ExceptionRetryConfiguration(
            errors=(requests.ConnectionError, requests.HTTPError, requests.Timeout), retries=3, backoff_factor=0.5)
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}, exception_retry_configuration=retry))

    def _pick_rpc(self, rpc_url: str) -> str:
        """User-provided URL wins; otherwise the first preset RPC that answers."""
        if rpc_url:
            return rpc_url
        candidates = (self.chain.default_rpc, *self.chain.fallback_rpcs)
        for url in candidates:
            try:
                if self._make_w3(url).eth.chain_id == self.chain.chain_id:
                    return url
            except Exception:
                continue
        return self.chain.default_rpc

    # ---- metadata ------------------------------------------------------------
    def connected(self) -> bool:
        try:
            return self.w3.is_connected() and self.w3.eth.chain_id == self.chain.chain_id
        except Exception:
            return False

    @property
    def pool(self) -> Contract:
        """Pool contract, resolved from the factory on first use."""
        if self._pool is None:
            addr = self.factory.functions.getPool(Web3.to_checksum_address(self.chain.weth),
                                                  Web3.to_checksum_address(self.chain.usdc),
                                                  self.pool_preset.fee).call()
            if int(addr, 16) == 0:
                raise ValueError(f"No {self.pool_preset.name} pool on {self.chain.name}")
            self._pool_address = Web3.to_checksum_address(addr)
            self._pool = self.w3.eth.contract(self._pool_address, abi=abis.POOL)
        return self._pool

    @property
    def pool_address(self) -> str:
        return self.pool.address

    @property
    def meta(self) -> PoolMeta:
        if self._meta is None:
            self._token0 = self.pool.functions.token0().call()
            self._token1 = self.pool.functions.token1().call()
            eth_is_token0 = self._token0.lower() == self.chain.weth.lower()
            if not eth_is_token0 and self._token1.lower() != self.chain.weth.lower():
                raise ValueError("Pool does not contain WETH")
            fee = self.pool.functions.fee().call()
            spacing = TICK_SPACING_BY_FEE.get(fee) or self.pool.functions.tickSpacing().call()
            self._meta = PoolMeta(eth_is_token0=eth_is_token0, eth_decimals=18,
                                  usd_decimals=self.chain.usdc_decimals, tick_spacing=spacing)
        return self._meta

    # ---- reads ----------------------------------------------------------------
    def price(self) -> float:
        slot0 = self.pool.functions.slot0().call()
        return self.meta.price_from_sqrt_x96(slot0[0])

    def balances(self) -> dict:
        addr = self.wallet.address
        if not addr:
            return {"eth": 0.0, "weth": 0.0, "usdc": 0.0}
        addr = Web3.to_checksum_address(addr)
        eth = self.w3.eth.get_balance(addr) / 1e18
        weth = self.weth.functions.balanceOf(addr).call() / 1e18
        usdc = self.usdc.functions.balanceOf(addr).call() / 10**self.chain.usdc_decimals
        return {"eth": eth, "weth": weth, "usdc": usdc}

    def owned_position_ids(self) -> list[int]:
        """Token IDs owned by the wallet that belong to our pool (token pair + fee)."""
        self.meta  # ensure token0/token1 are resolved
        addr = Web3.to_checksum_address(self.wallet.address)
        n = self.npm.functions.balanceOf(addr).call()
        ids = []
        for i in range(min(n, 50)):
            tid = self.npm.functions.tokenOfOwnerByIndex(addr, i).call()
            pos = self.npm.functions.positions(tid).call()
            if (pos[2].lower(), pos[3].lower(), pos[4]) == (self._token0.lower(), self._token1.lower(),
                                                              self.pool_preset.fee) and pos[7] > 0:
                ids.append(tid)
        return ids

    def position(self, token_id: int) -> Optional[LPPosition]:
        pos = self.npm.functions.positions(token_id).call()
        if pos[7] == 0:
            return None
        return LPPosition(liquidity=pos[7], tick_lower=pos[5], tick_upper=pos[6], meta=self.meta)

    def pending_fees(self, token_id: int) -> tuple[float, float]:
        """Simulate collect() to learn uncollected fees (eth, usd)."""
        addr = Web3.to_checksum_address(self.wallet.address)
        try:
            a0, a1 = self.npm.functions.collect((token_id, addr, MAX_UINT128, MAX_UINT128)).call({"from": addr})
        except Exception:
            return 0.0, 0.0
        return self.meta.to_human(a0, a1)

    # ---- writes ---------------------------------------------------------------
    def _send(self, fn, description: str, value: int = 0) -> TxResult:
        addr = Web3.to_checksum_address(self.wallet.address)
        tx = {"from": addr, "value": value, "chainId": self.chain.chain_id}
        gas = fn.estimate_gas(tx)
        if self.dry_run:
            self.log("info", f"[dry-run] would send: {description} (gas≈{gas})")
            return TxResult(None, True, gas, description)
        base = self.w3.eth.gas_price
        try:
            prio = max(self.w3.eth.max_priority_fee, 1)
        except Exception:  # RPC without eth_maxPriorityFeePerGas
            prio = 10**8
        tx.update({
            "nonce": self.w3.eth.get_transaction_count(addr, "pending"),
            "gas": int(gas * 1.3),
            "maxFeePerGas": int(base * 2 + prio),
            "maxPriorityFeePerGas": int(prio),
        })
        built = fn.build_transaction(tx)
        signed = self.wallet.account.sign_transaction(built)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.log("info", f"sent {description}: {h.hex()}")
        receipt = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {description} ({h.hex()})")
        return TxResult(h.hex(), False, receipt["gasUsed"], description, dict(receipt))

    def ensure_allowance(self, token: Contract, spender: str, amount: int) -> Optional[TxResult]:
        addr = Web3.to_checksum_address(self.wallet.address)
        if token.functions.allowance(addr, spender).call() >= amount:
            return None
        return self._send(token.functions.approve(spender, MAX_UINT256),
                          f"approve {token.address[:8]}… for {spender[:8]}…")

    def wrap_eth(self, amount_wei: int) -> TxResult:
        return self._send(self.weth.functions.deposit(), f"wrap {amount_wei / 1e18:.6f} ETH", value=amount_wei)

    def unwrap_weth(self, amount_wei: int) -> TxResult:
        return self._send(self.weth.functions.withdraw(amount_wei), f"unwrap {amount_wei / 1e18:.6f} WETH")

    def open_position(self, deploy_usd: float, down_pct: float, up_pct: float,
                      gas_reserve_eth: float) -> dict:
        """Mint a new position centred on the current price. Returns the plan + tx."""
        price = self.price()
        meta = self.meta
        tick_lower, tick_upper = meta.range_ticks(price, down_pct, up_pct)
        bal = self.balances()
        eth_avail = max(0.0, bal["eth"] - gas_reserve_eth) + bal["weth"]
        plan = plan_mint(meta, price, tick_lower, tick_upper, eth_avail, bal["usdc"], deploy_usd)
        plan.update({"price": price, "tick_lower": tick_lower, "tick_upper": tick_upper,
                     "price_low": meta.price_bounds(tick_lower, tick_upper)[0],
                     "price_high": meta.price_bounds(tick_lower, tick_upper)[1]})
        if plan["liquidity"] <= 0:
            raise ValueError(
                f"Not enough funds. Need ≈{plan['need_eth']:.5f} ETH and ≈{plan['need_usd']:.2f} USDC "
                f"for ${deploy_usd:,.0f}; wallet has {eth_avail:.5f} ETH (after gas reserve) and "
                f"{bal['usdc']:.2f} USDC.")
        if plan["value_usd"] < deploy_usd * 0.98:
            self.log("warn", f"Wallet only covers ${plan['value_usd']:,.0f} of the ${deploy_usd:,.0f} target "
                             f"(short {plan['short_eth']:.5f} ETH / {plan['short_usd']:.2f} USDC). Minting what fits.")

        # Wrap ETH if WETH is insufficient
        need_weth = int(Decimal(str(plan["eth"])) * 10**18)
        have_weth = int(Decimal(str(bal["weth"])) * 10**18)
        txs = []
        if need_weth > have_weth:
            txs.append(self.wrap_eth(need_weth - have_weth))
        npm_addr = self.npm.address
        a0, a1 = plan["amount0"], plan["amount1"]
        for tok, amt in ((self.w3.eth.contract(Web3.to_checksum_address(self._token0), abi=abis.ERC20), a0),
                         (self.w3.eth.contract(Web3.to_checksum_address(self._token1), abi=abis.ERC20), a1)):
            if amt > 0:
                r = self.ensure_allowance(tok, npm_addr, amt)
                if r:
                    txs.append(r)
        params = (
            Web3.to_checksum_address(self._token0), Web3.to_checksum_address(self._token1),
            self.pool_preset.fee, tick_lower, tick_upper, a0, a1,
            int(a0 * (1 - self.slippage)), int(a1 * (1 - self.slippage)),
            Web3.to_checksum_address(self.wallet.address), int(time.time()) + 600,
        )
        if self.dry_run and (need_weth > have_weth or any(t is not None for t in txs)):
            # Mint estimate would revert without the (unsent) wrap/approve; report plan only.
            self.log("info", f"[dry-run] would mint {plan['eth']:.5f} ETH + {plan['usd']:.2f} USDC "
                             f"in [{plan['price_low']:.0f}, {plan['price_high']:.0f}]")
            tx = TxResult(None, True, 0, "mint (not simulated: depends on unsent approvals)")
        else:
            tx = self._send(self.npm.functions.mint(params),
                            f"mint {plan['eth']:.5f} ETH + {plan['usd']:.2f} USDC "
                            f"in [{plan['price_low']:.0f}, {plan['price_high']:.0f}]")
        token_id = None
        if tx.receipt:
            token_id = self._token_id_from_receipt(tx.receipt)
        return {"plan": plan, "tx": tx, "token_id": token_id, "prep_txs": txs}

    def _token_id_from_receipt(self, receipt: dict) -> Optional[int]:
        transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)")
        for lg in receipt.get("logs", []):
            if lg["address"].lower() == self.npm.address.lower() and lg["topics"][0] == transfer_topic:
                return int.from_bytes(bytes(lg["topics"][3]), "big")
        return None

    def close_position(self, token_id: int, unwrap: bool = True) -> list[TxResult]:
        """Remove all liquidity, collect everything, burn the NFT."""
        pos = self.position(token_id)
        results = []
        addr = Web3.to_checksum_address(self.wallet.address)
        if pos is not None:
            price = self.price()
            sp = self.meta.raw_sqrt_from_price(price)
            a0, a1 = amounts_for_liquidity(pos.liquidity, sp, sqrt_ratio_at_tick(pos.tick_lower),
                                           sqrt_ratio_at_tick(pos.tick_upper))
            results.append(self._send(
                self.npm.functions.decreaseLiquidity((token_id, pos.liquidity, int(a0 * (1 - self.slippage)),
                                                      int(a1 * (1 - self.slippage)), int(time.time()) + 600)),
                f"remove liquidity from #{token_id}"))
        if not self.dry_run or pos is None:
            results.append(self._send(self.npm.functions.collect((token_id, addr, MAX_UINT128, MAX_UINT128)),
                                      f"collect from #{token_id}"))
            try:
                results.append(self._send(self.npm.functions.burn(token_id), f"burn #{token_id}"))
            except Exception as e:  # burn is cosmetic; don't fail the close
                self.log("warn", f"burn skipped: {e}")
            if unwrap and not self.dry_run:
                weth = self.weth.functions.balanceOf(addr).call()
                if weth > 0:
                    results.append(self.unwrap_weth(weth))
        else:
            self.log("info", f"[dry-run] would collect + burn #{token_id} after removing liquidity")
        return results

    def collect_fees(self, token_id: int) -> TxResult:
        addr = Web3.to_checksum_address(self.wallet.address)
        return self._send(self.npm.functions.collect((token_id, addr, MAX_UINT128, MAX_UINT128)),
                          f"collect fees from #{token_id}")

    def swap(self, sell_eth: bool, amount_in_human: float, price: float) -> TxResult:
        """Swap WETH->USDC or USDC->WETH through the pool's fee tier."""
        if sell_eth:
            token_in, token_out = self.weth, self.usdc
            amount_in = int(Decimal(str(amount_in_human)) * 10**18)
            min_out = int(Decimal(str(amount_in_human * price * (1 - self.slippage))) * 10**self.chain.usdc_decimals)
            desc = f"swap {amount_in_human:.5f} WETH -> USDC"
        else:
            token_in, token_out = self.usdc, self.weth
            amount_in = int(Decimal(str(amount_in_human)) * 10**self.chain.usdc_decimals)
            min_out = int(Decimal(str(amount_in_human / price * (1 - self.slippage))) * 10**18)
            desc = f"swap {amount_in_human:.2f} USDC -> WETH"
        self.ensure_allowance(token_in, self.router.address, amount_in)
        params = (token_in.address, token_out.address, self.pool_preset.fee,
                  Web3.to_checksum_address(self.wallet.address), amount_in, min_out, 0)
        return self._send(self.router.functions.exactInputSingle(params), desc)

    def rebalance_to_ratio(self, eth_target: float, usd_target: float, gas_reserve_eth: float) -> list[TxResult]:
        """Swap so the wallet holds roughly (eth_target, usd_target), ignoring gas reserve."""
        price = self.price()
        bal = self.balances()
        txs = []
        weth_total = bal["weth"] + max(0.0, bal["eth"] - gas_reserve_eth)
        if bal["weth"] < weth_total * 0.999 and weth_total > 0:
            txs.append(self.wrap_eth(int((weth_total - bal["weth"]) * 1e18)))
        eth_excess = weth_total - eth_target
        if eth_excess * price > 5:          # more than $5 of ETH too much -> sell
            txs.append(self.swap(True, eth_excess, price))
        elif -eth_excess * price > 5:       # need more ETH -> buy with USDC
            spend = min(bal["usdc"], -eth_excess * price)
            if spend > 1:
                txs.append(self.swap(False, spend, price))
        return txs
