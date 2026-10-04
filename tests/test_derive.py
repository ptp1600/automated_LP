from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from lp_hedger.derive import (
    PROFILES, DeriveClient, Instrument, Ticker, action_nonce, parse_option_name, round_step, to_e18, wire_decimal,
)
from lp_hedger.wallet import HotWallet


class MemWallet(HotWallet):
    """Wallet backed by an in-memory key (no keystore file)."""

    def __init__(self, key):
        super().__init__()
        self._account = Account.from_key(key)


KEY = "0x" + "ab" * 32
OWNER = "0x" + "cd" * 20
INST = Instrument(name="ETH-20261030-2250-P", strike=2250, expiry=1793347200, option_type="P", is_active=True,
                  base_asset_address="0x4BB4C3CDc7562f08e9910A0C7D8bB7e108861eB4", base_asset_sub_id=39614081901377263198565322368,
                  tick_size=Decimal("0.1"), amount_step=Decimal("0.01"), minimum_amount=Decimal("0.1"),
                  taker_fee_rate=0.0003, base_fee=0.5)


def client(version="v2", env="mainnet", sub=777, **kw):
    return DeriveClient.from_settings(version, env, MemWallet(KEY), OWNER, sub, **kw)


def test_round_step_and_wire_precision():
    assert round_step(Decimal("1.2345"), Decimal("0.1")) == Decimal("1.2")
    assert round_step(Decimal("1.2345"), Decimal("0.1"), up=True) == Decimal("1.3")
    assert wire_decimal(Decimal("0.1234567890123456")) == Decimal("0.123456789012")
    assert to_e18(Decimal("0.1234567890123456")) % 10**6 == 0   # v3 rule: no sub-1e12 precision


def test_nonce_formats():
    assert 15 <= len(str(action_nonce("v2"))) <= 17      # ms + 3 digits
    assert len(str(action_nonce("v3"))) == 19            # nanoseconds


def test_parse_option_name():
    assert parse_option_name("ETH-20261030-2250-P") == (2250.0, 1793347200, "P")
    assert parse_option_name("ETH-20261030-2400_5-C")[0] == 2400.5


@pytest.mark.parametrize("profile", ["v2-mainnet", "v2-testnet", "v3-mainnet", "v3-testnet"])
def test_signature_matches_official_package(profile):
    """Hashing is identical across generations; only the constants differ."""
    pytest.importorskip("derive_action_signing")
    from derive_action_signing import SignedAction, TradeModuleData

    version, env = profile.split("-")
    c = client(version, env)
    order = c.sign_order(INST, "buy", Decimal("41.5"), Decimal("0.7"), Decimal("3.00"), nonce=1695836058725001,
                         expiry_sec=1900000000)
    ref = SignedAction(
        subaccount_id=777, owner=OWNER, signer=c.wallet.address, signature_expiry_sec=1900000000,
        nonce=1695836058725001, module_address=PROFILES[profile].trade_module,
        module_data=TradeModuleData(asset_address=INST.base_asset_address, sub_id=INST.base_asset_sub_id,
                                    limit_price=Decimal("41.5"), amount=Decimal("0.7"), max_fee=Decimal("3.00"),
                                    recipient_id=777, is_bid=True),
        DOMAIN_SEPARATOR=PROFILES[profile].domain_separator, ACTION_TYPEHASH=c.p.domain_separator and
        "0x4d7a9f27c403ff9c0f19bce61d76d82f9aa29f8d6d4b0c5474607d9770d1af17",
    )
    ref.sign(KEY)
    assert order["signature"].lower().replace("0x", "") == ref.signature.lower().replace("0x", "")
    assert order["limit_price"] == "41.5" and order["amount"] == "0.7" and order["direction"] == "buy"
    assert order["time_in_force"] == "ioc" and order["signer"] == c.wallet.address
    if version == "v3":
        assert isinstance(order["nonce"], str)          # > 2^53, must not be a JSON number
    else:
        assert isinstance(order["nonce"], int)


def test_signatures_differ_between_generations():
    kw = dict(nonce=1695836058725001, expiry_sec=1900000000)
    a = client("v2").sign_order(INST, "buy", Decimal("41.5"), Decimal("0.7"), Decimal("3"), **kw)["signature"]
    b = client("v3").sign_order(INST, "buy", Decimal("41.5"), Decimal("0.7"), Decimal("3"), **kw)["signature"]
    assert a != b


@pytest.mark.parametrize("version,prefix", [("v2", "X-Lyra"), ("v3", "X-Derive")])
def test_auth_header_recovers_signer(version, prefix):
    c = client(version, "testnet", 1)
    h = c.auth_headers()
    rec = Account.recover_message(encode_defunct(text=h[f"{prefix}Timestamp"]), signature=h[f"{prefix}Signature"])
    assert rec == c.wallet.address and h[f"{prefix}Wallet"] == OWNER
    assert set(h) == {f"{prefix}Wallet", f"{prefix}Timestamp", f"{prefix}Signature"}


def test_ticker_parsing_both_shapes():
    v2 = Ticker.from_api({"best_bid_price": "38", "best_ask_price": "40", "best_bid_amount": "5", "best_ask_amount": "6",
                          "mark_price": "39", "index_price": "2500", "min_price": "30", "max_price": "50",
                          "option_pricing": {"delta": "-0.3", "iv": "0.6"}, "taker_fee_rate": "0.0003"}, INST, "v2")
    v3 = Ticker.from_api({"b": "38", "a": "40", "B": "5", "A": "6", "M": "39", "I": "2500", "minp": "30", "maxp": "50",
                          "option_pricing": {"d": "-0.3", "i": "0.6", "m": "39"}, "t": 1}, INST, "v3")
    for t in (v2, v3):
        assert (t.best_bid, t.best_ask, t.bid_size, t.ask_size, t.mark_price, t.index_price) == (38, 40, 5, 6, 39, 2500)
        assert (t.delta, t.iv, t.min_price, t.max_price) == (-0.3, 0.6, 30, 50)


def test_place_ioc_dry_run_prices_rounds_and_clamps_to_band():
    c = client("v3", "testnet", 5, dry_run=True)
    tk = Ticker(INST, best_bid=38.0, best_ask=40.0, bid_size=5, ask_size=5, mark_price=39, index_price=2500,
                delta=-0.3, iv=0.6, taker_fee_rate=0.0003, min_price=37.5, max_price=40.8)
    res = c.place_ioc(tk, "buy", 0.7777, slippage_pct=3.0)
    assert res["dry_run"] and res["filled"] == pytest.approx(0.77)
    assert Decimal(res["order"]["limit_price"]) == Decimal("40.8")   # 41.2 clamped to the max price band
    res = c.place_ioc(tk, "sell", 0.7, slippage_pct=3.0)
    assert Decimal(res["order"]["limit_price"]) == Decimal("37.5")   # 36.8 clamped up to the min band
    assert res["order"]["reduce_only"] is True
    assert Decimal(res["order"]["max_fee"]) >= Decimal("1")
    with pytest.raises(Exception):
        c.place_ioc(tk, "buy", 0.01, 1.0)


def test_positions_parse(monkeypatch):
    c = client("v2", "mainnet", 5)
    monkeypatch.setattr(c, "_post", lambda *a, **k: {"positions": [
        {"instrument_name": "ETH-20261030-2250-P", "instrument_type": "option", "amount": "0.7",
         "average_price": "40", "mark_price": "42", "delta": "-0.3"},
        {"instrument_name": "ETH-PERP", "instrument_type": "perp", "amount": "1"},
        {"instrument_name": "BTC-20261030-60000-P", "instrument_type": "option", "amount": "1"},
    ]})
    ps = c.get_positions("ETH")
    assert len(ps) == 1 and ps[0].strike == 2250 and ps[0].option_type == "P"
    assert ps[0].expiry == 1793347200  # 2026-10-30 08:00 UTC


def test_v3_instruments_paginate_and_tolerate_empty(monkeypatch):
    c = client("v3", "testnet", 5)
    calls = []

    def fake_post(path, payload, **kw):
        calls.append(payload.get("page"))
        if payload["page"] == 3:
            from lp_hedger.derive import DeriveError
            raise DeriveError("public/get_all_instruments: instrument_not_found 12001")
        inst = {"instrument_name": f"ETH-20261030-{2000 + payload['page']}-P", "base_asset_address": INST.base_asset_address,
                "base_asset_sub_id": "1", "option_details": {"strike": "2250", "expiry": 1793347200, "option_type": "P"}}
        return {"instruments": [inst], "pagination": {"num_pages": 3, "count": 3}}

    monkeypatch.setattr(c, "_post", fake_post)
    insts = c.get_instruments("ETH")
    assert calls == [1, 2, 3] and len(insts) == 2


def test_verify_connection_suggests_subaccount(monkeypatch):
    c = client("v3", "testnet", 0)

    def fake_post(path, payload, **kw):
        if path == "private/get_subaccounts":
            return {"subaccount_ids": [10, 11], "wallet": OWNER}
        if path == "private/get_subaccount":
            return {"manager_id": 0 if payload["subaccount_id"] == 10 else 3, "collaterals": [{"mark_value": "100"}]}
        raise AssertionError(path)

    monkeypatch.setattr(c, "_post", fake_post)
    assert c.discover_subaccount() == 11   # skips the manager-0 fallback subaccount
    with pytest.raises(Exception, match="Suggested: 11"):
        c.verify_connection()
    c.subaccount_id = 11
    assert c.verify_connection()["collateral_usd"] == 100


def test_deposit_is_v3_only():
    with pytest.raises(Exception, match="v3"):
        client("v2").deposit_collateral(10, "http://localhost:1")
