"""Derive (formerly Lyra) options client supporting both API generations.

* **v2** — the API live on mainnet today (``api.lyra.finance``). Accounts are
  smart-contract "Derive wallets" on Derive Chain; the hot wallet signs as the
  owner EOA or as a registered session key. Auth headers are ``X-Lyra*``.
* **v3** — the new API (``api.derive.xyz/v3``) settling on Ethereum L1 with ZK
  proofs. Accounts belong directly to an EOA, so the hot wallet can be the
  owner. Live on testnet (Sepolia); mainnet launches later. Auth headers are
  ``X-Derive*``, nonces are nanoseconds, and e18 words must not carry more
  than 12 decimals.

Trading in both generations is an EIP-712 ``Action`` signed over the trade
module data. Constants below come from Derive's published Protocol Constants /
Contracts pages and match the official ``derive_action_signing`` (v2) and
``derive-py`` (v3) packages.
"""
from __future__ import annotations

import calendar
import random
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Callable, Optional

import requests
from eth_abi.abi import encode
from web3 import Web3

from .wallet import HotWallet

ACTION_TYPEHASH = "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17"
TRADE_MODULE_V3 = "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b"
WIRE_DECIMALS = 12   # v3 rejects e18 words with sub-1e12 precision


@dataclass(frozen=True)
class ApiProfile:
    version: str                # "v2" | "v3"
    environment: str            # "mainnet" | "testnet"
    base_url: str
    app_url: str
    header_prefix: str          # "X-Lyra" | "X-Derive"
    domain_separator: str
    trade_module: str
    settlement_chain_id: int    # where collateral lives (Derive Chain for v2, Ethereum for v3)
    action_manager: str = ""    # v3 L1 deposit contract (empty = unknown / not launched)

    @property
    def key(self) -> str:
        return f"{self.version}-{self.environment}"


PROFILES: dict[str, ApiProfile] = {
    "v2-mainnet": ApiProfile(
        "v2", "mainnet", "https://api.lyra.finance", "https://www.derive.xyz", "X-Lyra",
        "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b",
        "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b", 957),
    "v2-testnet": ApiProfile(
        "v2", "testnet", "https://api-demo.lyra.finance", "https://testnet.derive.xyz", "X-Lyra",
        "0x9bcf4dc06df5d8bf23af818d5716491b995020f377d3b7b64c29ed14e3dd1105",
        "0x87F2863866D85E3192a35A73b388BD625D83f2be", 901),
    "v3-mainnet": ApiProfile(
        "v3", "mainnet", "https://api.derive.xyz/v3", "https://app.derive.xyz", "X-Derive",
        "0xda616dfabb88681b08e1592820a41d55ddc62d68de110e327ae99d734506fe19",
        TRADE_MODULE_V3, 1, "0xE366CcA474968e33b777E13905829A3b800CFAD3"),
    "v3-testnet": ApiProfile(
        "v3", "testnet", "https://testnet.api.derive.xyz/v3", "https://testnet.app.derive.xyz", "X-Derive",
        "0x24d674cd5f2b9d564691c51e9d88f649b99246a2244dd74ce27b96578d773e85",
        TRADE_MODULE_V3, 11155111, "0xd3625eCf97E5554C62A48Ac1c9284C9dCeFceB68"),
}

PUBLIC_HEADERS = {"accept": "application/json", "content-type": "application/json"}

ACTION_MANAGER_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "subaccountId", "type": "uint64"}, {"name": "fallbackRecipient", "type": "address"}],
     "outputs": [{"name": "actionId", "type": "uint256"}]},
    {"name": "depositToNewSubaccount", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "managerId", "type": "uint32"}, {"name": "owner_", "type": "address"}],
     "outputs": [{"name": "actionId", "type": "uint256"}]},
]


class DeriveError(RuntimeError):
    pass


# ---- data model ------------------------------------------------------------------

@dataclass
class Instrument:
    name: str
    strike: float
    expiry: int            # unix seconds
    option_type: str       # "P" or "C"
    is_active: bool
    base_asset_address: str
    base_asset_sub_id: int
    tick_size: Decimal
    amount_step: Decimal
    minimum_amount: Decimal
    taker_fee_rate: float = 0.0003
    base_fee: float = 0.0

    @property
    def days_to_expiry(self) -> float:
        return (self.expiry - time.time()) / 86400.0

    @staticmethod
    def from_api(d: dict) -> "Instrument":
        od = d.get("option_details") or {}
        return Instrument(
            name=d["instrument_name"],
            strike=float(od.get("strike", 0) or 0),
            expiry=int(od.get("expiry", 0) or 0),
            option_type=od.get("option_type", "?"),
            is_active=bool(d.get("is_active", True)),
            base_asset_address=d["base_asset_address"],
            base_asset_sub_id=int(d["base_asset_sub_id"]),
            tick_size=Decimal(str(d.get("tick_size", "0.1"))),
            amount_step=Decimal(str(d.get("amount_step", "0.1"))),
            minimum_amount=Decimal(str(d.get("minimum_amount", "0.1"))),
            taker_fee_rate=float(d.get("taker_fee_rate") or 0.0003),
            base_fee=float(d.get("base_fee") or 0),
        )


@dataclass
class Ticker:
    instrument: Instrument
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    mark_price: float
    index_price: float
    delta: float
    iv: float
    taker_fee_rate: float
    min_price: float = 0.0      # price band: aggressive sells below this are cancelled
    max_price: float = 0.0      # price band: aggressive buys above this are cancelled

    @staticmethod
    def from_api(d: dict, inst: Instrument, version: str = "v2") -> "Ticker":
        op = d.get("option_pricing") or {}
        f = lambda v: float(v or 0)
        if version == "v3":  # slim ticker: short keys
            return Ticker(inst, f(d.get("b")), f(d.get("a")), f(d.get("B")), f(d.get("A")),
                          f(d.get("M")) or f(op.get("m")), f(d.get("I")), f(op.get("d")), f(op.get("i")),
                          inst.taker_fee_rate, f(d.get("minp")), f(d.get("maxp")))
        return Ticker(inst, f(d.get("best_bid_price")), f(d.get("best_ask_price")),
                      f(d.get("best_bid_amount")), f(d.get("best_ask_amount")),
                      f(d.get("mark_price")) or f(op.get("mark_price")), f(d.get("index_price")),
                      f(op.get("delta")), f(op.get("iv")), f(d.get("taker_fee_rate")) or inst.taker_fee_rate,
                      f(d.get("min_price")), f(d.get("max_price")))


@dataclass
class OptionPosition:
    instrument_name: str
    amount: float          # signed: + long, - short
    average_price: float
    mark_price: float
    delta: float
    strike: float
    expiry: int
    option_type: str

    @property
    def days_to_expiry(self) -> float:
        return (self.expiry - time.time()) / 86400.0


# ---- helpers ------------------------------------------------------------------------

def round_step(value: Decimal, step: Decimal, up: bool = False) -> Decimal:
    if step <= 0:
        return value
    q = (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return (q * step).quantize(step)


def wire_decimal(value: Decimal) -> Decimal:
    """Clamp to the 12 fractional digits the protocol accepts (v3 rejects more)."""
    return value.quantize(Decimal(1).scaleb(-WIRE_DECIMALS), rounding=ROUND_DOWN).normalize()


def to_e18(v: Decimal) -> int:
    return int(wire_decimal(v) * Decimal(10**18))


def action_nonce(version: str) -> int:
    if version == "v3":
        return time.time_ns()                                   # ~19 digits
    return int(f"{int(time.time() * 1000)}{random.randint(0, 999):03d}")  # v2: ms + 3 digits


def parse_option_name(name: str) -> tuple[float, int, str]:
    """ETH-20261030-2250-P -> (strike, expiry_ts, 'P'). Options settle 08:00 UTC."""
    parts = name.split("-")
    strike = float(parts[2].replace("_", "."))
    expiry = calendar.timegm(time.strptime(parts[1], "%Y%m%d")) + 8 * 3600
    return strike, expiry, parts[3]


# ---- client -------------------------------------------------------------------------

class DeriveClient:
    def __init__(self, profile: ApiProfile, wallet: Optional[HotWallet], owner: str, subaccount_id: int,
                 dry_run: bool = True, log: Optional[Callable[[str, str], None]] = None,
                 session: Optional[requests.Session] = None):
        self.p = profile
        self.wallet = wallet        # None is fine for public market data (paper trading)
        self.owner = owner          # v2: Derive smart-contract wallet; v3: owning EOA
        self.subaccount_id = int(subaccount_id)
        self.dry_run = dry_run
        self.log = log or (lambda level, msg: None)
        self.http = session or requests.Session()

    @classmethod
    def from_settings(cls, api_version: str, environment: str, wallet: Optional[HotWallet], owner: str,
                      subaccount_id: int, **kw) -> "DeriveClient":
        key = f"{api_version}-{environment}"
        if key not in PROFILES:
            raise ValueError(f"unknown Derive profile {key}; options: {list(PROFILES)}")
        return cls(PROFILES[key], wallet, owner, subaccount_id, **kw)

    @property
    def version(self) -> str:
        return self.p.version

    # ---- transport -----------------------------------------------------------
    def _post(self, path: str, payload: dict, private: bool = False, timeout: int = 20) -> Any:
        headers = dict(PUBLIC_HEADERS)
        if private:
            headers.update(self.auth_headers())
        r = self.http.post(f"{self.p.base_url}/{path}", json=payload, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            raise DeriveError(f"{path}: HTTP {r.status_code} non-JSON response")
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                raise DeriveError(f"{path}: {err.get('message', err)} {err.get('data', '') or ''}".strip(),)
            raise DeriveError(f"{path}: {err}")
        if r.status_code >= 400:
            raise DeriveError(f"{path}: HTTP {r.status_code} {body}")
        return body.get("result", body) if isinstance(body, dict) else body

    def auth_headers(self) -> dict[str, str]:
        if not self.owner or self.wallet is None:
            raise DeriveError("Derive wallet address not configured")
        ts = str(int(time.time() * 1000))
        px = self.p.header_prefix
        return {f"{px}Wallet": self.owner, f"{px}Timestamp": ts, f"{px}Signature": self.wallet.sign_message_text(ts)}

    # ---- public --------------------------------------------------------------
    def get_instruments(self, currency: str = "ETH", expired: bool = False) -> list[Instrument]:
        if self.version == "v3":
            out: list[Instrument] = []
            page = 1
            while True:
                try:
                    res = self._post("public/get_all_instruments",
                                     {"currency": currency, "instrument_type": "option", "expired": expired,
                                      "page": page, "page_size": 1000})
                except DeriveError as e:
                    if "instrument_not_found" in str(e) or "12001" in str(e):
                        break
                    raise
                out.extend(Instrument.from_api(d) for d in res.get("instruments", []))
                if page >= int((res.get("pagination") or {}).get("num_pages") or 1):
                    break
                page += 1
            return out
        res = self._post("public/get_instruments",
                         {"currency": currency, "instrument_type": "option", "expired": expired})
        return [Instrument.from_api(d) for d in res]

    def get_instrument(self, name: str) -> Instrument:
        return Instrument.from_api(self._post("public/get_instrument", {"instrument_name": name}))

    def get_ticker(self, inst: "Instrument | str") -> Ticker:
        if isinstance(inst, str):
            inst = self.get_instrument(inst)
        return Ticker.from_api(self._post("public/get_ticker", {"instrument_name": inst.name}), inst, self.version)

    def get_index_price(self, currency: str = "ETH") -> Optional[float]:
        try:
            d = self._post("public/get_ticker", {"instrument_name": f"{currency}-PERP"})
            return float((d.get("I") if self.version == "v3" else d.get("index_price")) or 0) or None
        except DeriveError:
            return None

    def get_risk_universes(self) -> list[dict]:
        return self._post("public/get_risk_universes", {})

    # ---- private -------------------------------------------------------------
    def get_subaccounts(self) -> dict:
        try:
            return self._post("private/get_subaccounts", {"wallet": self.owner}, private=True)
        except DeriveError as e:
            msg = str(e)
            if "Session key not found" in msg or "wallet not found" in msg.lower():
                hint = ("Derive does not know this wallet yet. "
                        + ("Deposit collateral from the hot wallet to create the account."
                           if self.version == "v3" else
                           "Check the Derive wallet address and register the hot wallet as a session key on derive.xyz."))
                raise DeriveError(f"{hint} ({msg})") from e
            raise

    def get_subaccount(self, subaccount_id: Optional[int] = None) -> dict:
        return self._post("private/get_subaccount", {"subaccount_id": subaccount_id or self.subaccount_id}, private=True)

    def collateral_usd(self, sub: Optional[dict] = None) -> float:
        sub = sub or self.get_subaccount()
        total = sum(float(c.get("mark_value") or 0) for c in sub.get("collaterals", []))
        return total or float(sub.get("subaccount_value") or 0)

    def get_positions(self, currency: str = "ETH") -> list[OptionPosition]:
        res = self._post("private/get_positions", {"subaccount_id": self.subaccount_id}, private=True)
        out = []
        for p in res.get("positions", []):
            name = p.get("instrument_name", "")
            if p.get("instrument_type") != "option" or not name.startswith(f"{currency}-"):
                continue
            amt = float(p.get("amount") or 0)
            if abs(amt) < 1e-12:
                continue
            try:
                strike, expiry, otype = parse_option_name(name)
            except (IndexError, ValueError):
                strike, expiry, otype = 0.0, 0, "?"
            out.append(OptionPosition(name, amt, float(p.get("average_price") or 0), float(p.get("mark_price") or 0),
                                      float(p.get("delta") or 0), strike, expiry, otype))
        return out

    def verify_connection(self) -> dict:
        """Check the hot wallet may sign for the owner wallet, and find a usable subaccount."""
        subs = self.get_subaccounts()
        ids = list(subs.get("subaccount_ids") or [s.get("subaccount_id") for s in subs.get("subaccounts", [])])
        if not ids:
            raise DeriveError(f"Wallet {self.owner} has no subaccounts on Derive {self.p.key}. Deposit collateral first.")
        suggested = None
        if self.subaccount_id not in ids:
            # v3 creates a manager-0 fallback subaccount on first deposit: skip it
            for sid in ids:
                try:
                    if int(self.get_subaccount(sid).get("manager_id", 1) or 0) != 0:
                        suggested = sid
                        break
                except DeriveError:
                    continue
            suggested = suggested or ids[0]
            raise DeriveError(f"Subaccount {self.subaccount_id} not found for this wallet. "
                              f"Available: {ids}. Suggested: {suggested}")
        sub = self.get_subaccount()
        return {"ok": True, "subaccount_ids": ids, "collateral_usd": self.collateral_usd(sub),
                "margin_type": sub.get("margin_type"), "currency": sub.get("currency"),
                "api": self.p.key, "owner": self.owner, "signer": self.wallet.address}

    def discover_subaccount(self) -> Optional[int]:
        """Best-effort pick of a tradeable subaccount id (non-fallback), or None."""
        try:
            ids = list(self.get_subaccounts().get("subaccount_ids") or [])
        except DeriveError:
            return None
        for sid in ids:
            try:
                if int(self.get_subaccount(sid).get("manager_id", 1) or 0) != 0:
                    return sid
            except DeriveError:
                continue
        return ids[0] if ids else None

    # ---- signing ---------------------------------------------------------------
    def trade_module_data(self, inst: Instrument, limit_price: Decimal, amount: Decimal, max_fee: Decimal,
                          is_bid: bool) -> bytes:
        return encode(
            ["address", "uint", "int", "int", "uint", "uint", "bool"],
            [Web3.to_checksum_address(inst.base_asset_address), inst.base_asset_sub_id,
             to_e18(limit_price), to_e18(amount), to_e18(max_fee), self.subaccount_id, is_bid],
        )

    def action_hash(self, module_data: bytes, nonce: int, expiry_sec: int, signer: str) -> bytes:
        return Web3.keccak(encode(
            ["bytes32", "uint", "uint", "address", "bytes32", "uint", "address", "address"],
            [bytes.fromhex(ACTION_TYPEHASH[2:]), self.subaccount_id, nonce,
             Web3.to_checksum_address(self.p.trade_module), Web3.keccak(module_data), expiry_sec,
             Web3.to_checksum_address(self.owner), Web3.to_checksum_address(signer)],
        ))

    def typed_data_hash(self, action_hash: bytes) -> bytes:
        return Web3.keccak(b"\x19\x01" + bytes.fromhex(self.p.domain_separator[2:]) + action_hash)

    def sign_order(self, inst: Instrument, direction: str, limit_price: Decimal, amount: Decimal,
                   max_fee: Decimal, nonce: Optional[int] = None, expiry_sec: Optional[int] = None) -> dict:
        signer = self.wallet.address
        nonce = nonce or action_nonce(self.version)
        expiry_sec = expiry_sec or int(time.time()) + 3600
        limit_price, amount, max_fee = wire_decimal(limit_price), wire_decimal(amount), wire_decimal(max_fee)
        data = self.trade_module_data(inst, limit_price, amount, max_fee, direction == "buy")
        digest = self.typed_data_hash(self.action_hash(data, nonce, expiry_sec, signer))
        return {
            "instrument_name": inst.name,
            "subaccount_id": self.subaccount_id,
            "direction": direction,
            "limit_price": f"{limit_price:f}",
            "amount": f"{amount:f}",
            "max_fee": f"{max_fee:f}",
            "nonce": str(nonce) if self.version == "v3" else nonce,   # v3: exceeds 2^53, must be a string
            "signer": signer,
            "signature_expiry_sec": expiry_sec,
            "signature": self.wallet.sign_hash(digest),
            "order_type": "limit",
            "time_in_force": "ioc",
            "mmp": False,
            "reduce_only": direction == "sell",
            "label": "lp-hedger",
        }

    # ---- orders ------------------------------------------------------------------
    def place_ioc(self, ticker: Ticker, direction: str, amount: float, slippage_pct: float) -> dict:
        """Immediate-or-cancel limit order crossing the spread by ``slippage_pct``,
        clamped to the exchange price band so it is not rejected as post-only."""
        inst = ticker.instrument
        amt = round_step(Decimal(str(amount)), inst.amount_step)
        if amt < inst.minimum_amount:
            raise DeriveError(f"amount {amt} below minimum {inst.minimum_amount} for {inst.name}")
        slip = Decimal(str(slippage_pct)) / 100
        if direction == "buy":
            if ticker.best_ask <= 0:
                raise DeriveError(f"no ask for {inst.name}")
            px = round_step(Decimal(str(ticker.best_ask)) * (1 + slip), inst.tick_size, up=True)
            if ticker.max_price > 0:
                px = min(px, round_step(Decimal(str(ticker.max_price)), inst.tick_size))
        else:
            if ticker.best_bid <= 0:
                raise DeriveError(f"no bid for {inst.name}")
            px = round_step(Decimal(str(ticker.best_bid)) * (1 - slip), inst.tick_size)
            if ticker.min_price > 0:
                px = max(px, round_step(Decimal(str(ticker.min_price)), inst.tick_size, up=True))
            px = max(px, inst.tick_size)
        # max_fee is a per-unit cap, not what you pay: 3x the taker rate on index, plus base fee
        fee_cap = Decimal(str(ticker.taker_fee_rate or 0.0003)) * 3 * Decimal(str(ticker.index_price or 1)) \
            + Decimal(str(inst.base_fee or 0))
        fee_cap = max(fee_cap, Decimal("1")).quantize(Decimal("0.01"))
        order = self.sign_order(inst, direction, px, amt, fee_cap)
        desc = f"{direction} {amt} {inst.name} @ {px} (IOC, {self.p.key})"
        if self.dry_run:
            self.log("info", f"[dry-run] would submit: {desc}")
            return {"dry_run": True, "order": order, "filled": float(amt), "avg_price": float(px),
                    "description": desc}
        res = self._post("private/order", order, private=True)
        o = res.get("order", res)
        filled = float(o.get("filled_amount") or 0)
        avg = float(o.get("average_price") or 0)
        self.log("info", f"submitted {desc}: status={o.get('order_status')} filled={filled} avg={avg}")
        return {"dry_run": False, "order": o, "filled": filled, "avg_price": avg, "description": desc,
                "trades": res.get("trades", [])}

    # ---- v3 on-chain deposit -----------------------------------------------------
    def choose_manager(self, currency: str = "ETH", collateral: str = "USDC", margin_type: str = "SM") -> dict:
        """Pick the manager that trades <currency>-OPTION and accepts <collateral>."""
        for u in self.get_risk_universes():
            for m in u.get("managers", []):
                if f"{currency}-OPTION" not in m.get("instruments", []):
                    continue
                col = next((c for c in m.get("collaterals", []) if c.get("name") == collateral), None)
                if col and (m.get("margin_type") == margin_type or margin_type == "any"):
                    return {"manager_id": int(m["manager_id"]), "margin_type": m.get("margin_type"),
                            "risk_universe_id": u.get("risk_universe_id"), "asset": col["address"],
                            "erc20": col["erc20"]["underlying_erc20_address"] if "underlying_erc20_address" in col["erc20"]
                            else col["erc20"]["underlying_erc20"],
                            "decimals": int(col["erc20"]["decimals"]), "min_deposit_usd": col.get("min_deposit_usd")}
        raise DeriveError(f"no manager trades {currency}-OPTION against {collateral}")

    def deposit_collateral(self, amount: float, rpc_url: str, currency: str = "ETH") -> dict:
        """v3 only: approve + deposit USDC from the hot wallet on the settlement chain.

        Funds an existing subaccount, or creates a new one under the chosen
        manager when ``subaccount_id`` is 0. Crediting is asynchronous (1-2 min).
        """
        if self.version != "v3":
            raise DeriveError("On-chain deposits are a v3 feature. On v2, deposit via the Derive web app.")
        if not self.p.action_manager:
            raise DeriveError("ACTION_MANAGER address unknown for this environment")
        if not rpc_url:
            raise DeriveError("Set a settlement-chain RPC URL (Ethereum / Sepolia) in Derive settings first")
        mgr = self.choose_manager(currency)
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        if w3.eth.chain_id != self.p.settlement_chain_id:
            raise DeriveError(f"RPC chain id {w3.eth.chain_id} != expected {self.p.settlement_chain_id}")
        from . import abis
        me = Web3.to_checksum_address(self.wallet.address)
        token = w3.eth.contract(Web3.to_checksum_address(mgr["erc20"]), abi=abis.ERC20)
        manager = w3.eth.contract(Web3.to_checksum_address(self.p.action_manager), abi=ACTION_MANAGER_ABI)
        raw = int(Decimal(str(amount)) * 10 ** mgr["decimals"])
        if token.functions.balanceOf(me).call() < raw:
            raise DeriveError(f"hot wallet holds less than {amount} USDC on chain {self.p.settlement_chain_id}")
        txs = []
        if token.functions.allowance(me, manager.address).call() < raw:
            txs.append(self._send_l1(w3, token.functions.approve(manager.address, raw), "approve USDC for Derive"))
        if self.subaccount_id > 0:
            fn = manager.functions.deposit(Web3.to_checksum_address(mgr["asset"]), raw, self.subaccount_id, me)
            desc = f"deposit {amount} USDC to Derive subaccount {self.subaccount_id}"
        else:
            fn = manager.functions.depositToNewSubaccount(Web3.to_checksum_address(mgr["asset"]), raw,
                                                           mgr["manager_id"], Web3.to_checksum_address(self.owner))
            desc = f"deposit {amount} USDC to a NEW Derive subaccount (manager {mgr['manager_id']}, {mgr['margin_type']})"
        txs.append(self._send_l1(w3, fn, desc))
        return {"manager": mgr, "txs": txs}

    def _send_l1(self, w3: Web3, fn, description: str) -> dict:
        me = Web3.to_checksum_address(self.wallet.address)
        tx = {"from": me, "chainId": w3.eth.chain_id}
        gas = fn.estimate_gas(tx)
        if self.dry_run:
            self.log("info", f"[dry-run] would send: {description} (gas≈{gas})")
            return {"dry_run": True, "description": description}
        base = w3.eth.gas_price
        try:
            prio = max(w3.eth.max_priority_fee, 1)
        except Exception:
            prio = 10**9
        tx.update({"nonce": w3.eth.get_transaction_count(me, "pending"), "gas": int(gas * 1.3),
                   "maxFeePerGas": int(base * 2 + prio), "maxPriorityFeePerGas": int(prio)})
        signed = self.wallet.account.sign_transaction(fn.build_transaction(tx))
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        self.log("info", f"sent {description}: {h.hex()}")
        rc = w3.eth.wait_for_transaction_receipt(h, timeout=300)
        if rc["status"] != 1:
            raise DeriveError(f"transaction reverted: {description} ({h.hex()})")
        return {"dry_run": False, "description": description, "tx_hash": h.hex()}
