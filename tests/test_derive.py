from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from lp_hedger.derive import DeriveClient, Instrument, OptionPosition, Ticker, round_step
from lp_hedger.wallet import HotWallet


class MemWallet(HotWallet):
    """Wallet backed by an in-memory key (no keystore file)."""

    def __init__(self, key):
        super().__init__()
        self._account = Account.from_key(key)


KEY = "0x" + "ab" * 32
DERIVE_WALLET = "0x" + "cd" * 20
INST = Instrument(name="ETH-20261030-2250-P", strike=2250, expiry=1793347200, option_type="P", is_active=True,
                  base_asset_address="0x4BB4C3CDc7562f08e9910A0C7D8bB7e108861eB4", base_asset_sub_id=123456789,
                  tick_size=Decimal("0.1"), amount_step=Decimal("0.1"), minimum_amount=Decimal("0.1"))


def test_round_step():
    assert round_step(Decimal("1.2345"), Decimal("0.1")) == Decimal("1.2")
    assert round_step(Decimal("1.2345"), Decimal("0.1"), up=True) == Decimal("1.3")
    assert round_step(Decimal("1.2"), Decimal("0.1")) == Decimal("1.2")


def test_signature_matches_official_package():
    derive_action_signing = pytest.importorskip("derive_action_signing")
    from derive_action_signing import SignedAction, TradeModuleData

    w = MemWallet(KEY)
    c = DeriveClient("mainnet", w, DERIVE_WALLET, 777)
    order = c.sign_order(INST, "buy", Decimal("41.5"), Decimal("0.7"), Decimal("3.00"), nonce=1695836058725001,
                         expiry_sec=1900000000)
    ref = SignedAction(
        subaccount_id=777, owner=DERIVE_WALLET, signer=w.address, signature_expiry_sec=1900000000,
        nonce=1695836058725001, module_address=c.c["trade_module"],
        module_data=TradeModuleData(asset_address=INST.base_asset_address, sub_id=INST.base_asset_sub_id,
                                    limit_price=Decimal("41.5"), amount=Decimal("0.7"), max_fee=Decimal("3.00"),
                                    recipient_id=777, is_bid=True),
        DOMAIN_SEPARATOR=c.c["domain_separator"], ACTION_TYPEHASH=c.c["action_typehash"],
    )
    ref.sign(KEY)
    assert order["signature"].lower().replace("0x", "") == ref.signature.lower().replace("0x", "")
    assert order["limit_price"] == "41.5" and order["amount"] == "0.7" and order["direction"] == "buy"
    assert order["time_in_force"] == "ioc" and order["signer"] == w.address


def test_auth_header_recovers_signer():
    w = MemWallet(KEY)
    c = DeriveClient("testnet", w, DERIVE_WALLET, 1)
    h = c.auth_headers()
    rec = Account.recover_message(encode_defunct(text=h["X-LyraTimestamp"]), signature=h["X-LyraSignature"])
    assert rec == w.address and h["X-LyraWallet"] == DERIVE_WALLET


def test_place_ioc_dry_run_prices_and_rounds():
    w = MemWallet(KEY)
    c = DeriveClient("mainnet", w, DERIVE_WALLET, 5, dry_run=True)
    tk = Ticker(INST, best_bid=38.0, best_ask=40.0, bid_size=5, ask_size=5, mark_price=39, index_price=2500,
                delta=-0.3, iv=0.6, taker_fee_rate=0.0003)
    res = c.place_ioc(tk, "buy", 0.7777, slippage_pct=3.0)
    assert res["dry_run"] and res["filled"] == pytest.approx(0.7)
    assert Decimal(res["order"]["limit_price"]) == Decimal("41.2")   # 40*1.03 rounded up to tick
    res = c.place_ioc(tk, "sell", 0.7, slippage_pct=3.0)
    assert Decimal(res["order"]["limit_price"]) == Decimal("36.8")   # 38*0.97 rounded down
    with pytest.raises(Exception):
        c.place_ioc(tk, "buy", 0.01, 1.0)


def test_positions_parse(monkeypatch):
    w = MemWallet(KEY)
    c = DeriveClient("mainnet", w, DERIVE_WALLET, 5)
    monkeypatch.setattr(c, "_post", lambda *a, **k: {"positions": [
        {"instrument_name": "ETH-20261030-2250-P", "instrument_type": "option", "amount": "0.7",
         "average_price": "40", "mark_price": "42", "delta": "-0.3"},
        {"instrument_name": "ETH-PERP", "instrument_type": "perp", "amount": "1"},
        {"instrument_name": "BTC-20261030-60000-P", "instrument_type": "option", "amount": "1"},
    ]})
    ps = c.get_positions("ETH")
    assert len(ps) == 1 and ps[0].strike == 2250 and ps[0].option_type == "P"
    assert ps[0].expiry == 1793347200  # 2026-10-30 08:00 UTC
