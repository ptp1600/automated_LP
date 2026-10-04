"""Derive (formerly Lyra) v2 options client.

Trading on Derive is off-chain order matching with on-chain self-custodial
settlement: every order is an EIP-712 "Action" signed by the owner or a
registered session key of the user's Derive smart-contract wallet.

Protocol constants come from Derive's published "Protocol Constants" table and
match the official ``derive_action_signing`` package and the open-source
``lyra_client``. They can be overridden via ``DeriveClient(constants=...)``.
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

MAX_INT_32 = 2**31 - 1

ENVIRONMENTS: dict[str, dict[str, str]] = {
    "mainnet": {
        "base_url": "https://api.lyra.finance",
        "ws_url": "wss://api.lyra.finance/ws",
        "chain_id": "957",
        "domain_separator": "0xd96e5f90797da7ec8dc4e276260c7f3f87fedf68775fbe1ef116e996fc60441b",
        "action_typehash": "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17",
        "trade_module": "0xB8D20c2B7a1Ad2EE33Bc50eF10876eD3035b5e7b",
        "app_url": "https://www.derive.xyz",
    },
    "testnet": {
        "base_url": "https://api-demo.lyra.finance",
        "ws_url": "wss://api-demo.lyra.finance/ws",
        "chain_id": "901",
        "domain_separator": "0x9bcf4dc06df5d8bf23af818d5716491b995020f377d3b7b64c29ed14e3dd1105",
        "action_typehash": "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17",
        "trade_module": "0x87F2863866D85E3192a35A73b388BD625D83f2be",
        "app_url": "https://testnet.derive.xyz",
    },
}

PUBLIC_HEADERS = {"accept": "application/json", "content-type": "application/json"}


class DeriveError(RuntimeError):
    pass


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

    @property
    def days_to_expiry(self) -> float:
        return (self.expiry - time.time()) / 86400.0

    @staticmethod
    def from_api(d: dict) -> "Instrument":
        od = d.get("option_details") or {}
        return Instrument(
            name=d["instrument_name"],
            strike=float(od.get("strike", 0)),
            expiry=int(od.get("expiry", 0)),
            option_type=od.get("option_type", "?"),
            is_active=bool(d.get("is_active", True)),
            base_asset_address=d["base_asset_address"],
            base_asset_sub_id=int(d["base_asset_sub_id"]),
            tick_size=Decimal(str(d.get("tick_size", "0.1"))),
            amount_step=Decimal(str(d.get("amount_step", "0.1"))),
            minimum_amount=Decimal(str(d.get("minimum_amount", "0.1"))),
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

    @staticmethod
    def from_api(d: dict) -> "Ticker":
        op = d.get("option_pricing") or {}
        return Ticker(
            instrument=Instrument.from_api(d),
            best_bid=float(d.get("best_bid_price") or 0),
            best_ask=float(d.get("best_ask_price") or 0),
            bid_size=float(d.get("best_bid_amount") or 0),
            ask_size=float(d.get("best_ask_amount") or 0),
            mark_price=float(d.get("mark_price") or op.get("mark_price") or 0),
            index_price=float(d.get("index_price") or 0),
            delta=float(op.get("delta") or 0),
            iv=float(op.get("iv") or 0),
            taker_fee_rate=float(d.get("taker_fee_rate") or 0),
        )


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


def round_step(value: Decimal, step: Decimal, up: bool = False) -> Decimal:
    if step <= 0:
        return value
    q = (value / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return (q * step).quantize(step)


def action_nonce() -> int:
    return int(f"{int(time.time() * 1000)}{random.randint(0, 999):03d}")


def to_e18(v: Decimal) -> int:
    return int(v * Decimal(10**18))


class DeriveClient:
    def __init__(self, environment: str, wallet: HotWallet, derive_wallet: str, subaccount_id: int,
                 dry_run: bool = True, log: Optional[Callable[[str, str], None]] = None,
                 constants: Optional[dict[str, str]] = None, session: Optional[requests.Session] = None):
        if environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {list(ENVIRONMENTS)}")
        self.env = environment
        self.c = {**ENVIRONMENTS[environment], **(constants or {})}
        self.wallet = wallet
        self.derive_wallet = derive_wallet
        self.subaccount_id = int(subaccount_id)
        self.dry_run = dry_run
        self.log = log or (lambda level, msg: None)
        self.http = session or requests.Session()

    # ---- transport -----------------------------------------------------------
    def _post(self, path: str, payload: dict, private: bool = False, timeout: int = 20) -> Any:
        headers = dict(PUBLIC_HEADERS)
        if private:
            headers.update(self.auth_headers())
        r = self.http.post(f"{self.c['base_url']}/{path}", json=payload, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except ValueError:
            raise DeriveError(f"{path}: HTTP {r.status_code} non-JSON response")
        if "error" in body and body["error"]:
            err = body["error"]
            raise DeriveError(f"{path}: {err.get('message', err)} {err.get('data', '')}".strip())
        if r.status_code >= 400:
            raise DeriveError(f"{path}: HTTP {r.status_code} {body}")
        return body.get("result", body)

    def auth_headers(self) -> dict[str, str]:
        if not self.derive_wallet:
            raise DeriveError("Derive wallet address not configured")
        ts = str(int(time.time() * 1000))
        return {
            "X-LyraWallet": self.derive_wallet,
            "X-LyraTimestamp": ts,
            "X-LyraSignature": self.wallet.sign_message_text(ts),
        }

    # ---- public --------------------------------------------------------------
    def get_instruments(self, currency: str = "ETH", expired: bool = False) -> list[Instrument]:
        res = self._post("public/get_instruments",
                         {"currency": currency, "instrument_type": "option", "expired": expired})
        return [Instrument.from_api(d) for d in res]

    def get_ticker(self, instrument_name: str) -> Ticker:
        return Ticker.from_api(self._post("public/get_ticker", {"instrument_name": instrument_name}))

    # ---- private -------------------------------------------------------------
    def get_subaccounts(self) -> dict:
        return self._post("private/get_subaccounts", {"wallet": self.derive_wallet}, private=True)

    def get_subaccount(self) -> dict:
        return self._post("private/get_subaccount", {"subaccount_id": self.subaccount_id}, private=True)

    def collateral_usd(self, sub: Optional[dict] = None) -> float:
        sub = sub or self.get_subaccount()
        total = 0.0
        for c in sub.get("collaterals", []):
            total += float(c.get("mark_value") or 0)
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
            parts = name.split("-")  # ETH-20261030-3000-P
            try:
                strike = float(parts[2]); otype = parts[3]
                # Derive options settle at 08:00 UTC on the expiry date
                expiry = calendar.timegm(time.strptime(parts[1], "%Y%m%d")) + 8 * 3600
            except (IndexError, ValueError):
                strike, otype, expiry = 0.0, "?", 0
            out.append(OptionPosition(
                instrument_name=name, amount=amt,
                average_price=float(p.get("average_price") or 0),
                mark_price=float(p.get("mark_price") or 0),
                delta=float(p.get("delta") or 0),
                strike=strike, expiry=expiry, option_type=otype,
            ))
        return out

    def verify_connection(self) -> dict:
        """Check the hot wallet is an authorised signer for the configured wallet/subaccount."""
        subs = self.get_subaccounts()
        ids = subs.get("subaccount_ids") or [s.get("subaccount_id") for s in subs.get("subaccounts", [])]
        if self.subaccount_id not in ids:
            raise DeriveError(f"Subaccount {self.subaccount_id} not found. Available: {ids}")
        sub = self.get_subaccount()
        return {"ok": True, "subaccount_ids": ids, "collateral_usd": self.collateral_usd(sub),
                "margin_type": sub.get("margin_type"), "currency": sub.get("currency")}

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
            [bytes.fromhex(self.c["action_typehash"][2:]), self.subaccount_id, nonce,
             Web3.to_checksum_address(self.c["trade_module"]), Web3.keccak(module_data), expiry_sec,
             Web3.to_checksum_address(self.derive_wallet), Web3.to_checksum_address(signer)],
        ))

    def typed_data_hash(self, action_hash: bytes) -> bytes:
        return Web3.keccak(b"\x19\x01" + bytes.fromhex(self.c["domain_separator"][2:]) + action_hash)

    def sign_order(self, inst: Instrument, direction: str, limit_price: Decimal, amount: Decimal,
                   max_fee: Decimal, nonce: Optional[int] = None, expiry_sec: Optional[int] = None) -> dict:
        signer = self.wallet.address
        nonce = nonce or action_nonce()
        expiry_sec = expiry_sec or int(time.time()) + 3600
        data = self.trade_module_data(inst, limit_price, amount, max_fee, direction == "buy")
        digest = self.typed_data_hash(self.action_hash(data, nonce, expiry_sec, signer))
        return {
            "instrument_name": inst.name,
            "subaccount_id": self.subaccount_id,
            "direction": direction,
            "limit_price": str(limit_price),
            "amount": str(amount),
            "max_fee": str(max_fee),
            "nonce": nonce,
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
        """Immediate-or-cancel limit order crossing the spread by ``slippage_pct``."""
        inst = ticker.instrument
        amt = round_step(Decimal(str(amount)), inst.amount_step)
        if amt < inst.minimum_amount:
            raise DeriveError(f"amount {amt} below minimum {inst.minimum_amount} for {inst.name}")
        if direction == "buy":
            if ticker.best_ask <= 0:
                raise DeriveError(f"no ask for {inst.name}")
            px = round_step(Decimal(str(ticker.best_ask)) * (1 + Decimal(str(slippage_pct)) / 100),
                            inst.tick_size, up=True)
        else:
            if ticker.best_bid <= 0:
                raise DeriveError(f"no bid for {inst.name}")
            px = round_step(Decimal(str(ticker.best_bid)) * (1 - Decimal(str(slippage_pct)) / 100),
                            inst.tick_size)
            px = max(px, inst.tick_size)
        # generous fee cap: 3x the quoted taker fee on notional, min $1
        fee_cap = max(Decimal("1"), Decimal(str(ticker.taker_fee_rate or 0.0005)) * 3
                      * Decimal(str(ticker.index_price or 1)) * amt)
        order = self.sign_order(inst, direction, px, amt, fee_cap.quantize(Decimal("0.01")))
        desc = f"{direction} {amt} {inst.name} @ {px} (IOC)"
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
