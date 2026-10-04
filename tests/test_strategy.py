import time
from decimal import Decimal

import pytest

from lp_hedger.config import HedgeSettings
from lp_hedger.derive import Instrument, OptionPosition, Ticker
from lp_hedger.strategy import compute_target, decide, pick_existing_hedge, scenario_table, select_instrument
from lp_hedger.uniswap_math import LPPosition, PoolMeta

META = PoolMeta(eth_is_token0=True, usd_decimals=6, tick_spacing=10)
NOW = time.time()
DAY = 86400


def inst(strike, days, otype="P"):
    return Instrument(name=f"ETH-D{days}-{int(strike)}-{otype}", strike=strike, expiry=int(NOW + days * DAY),
                      option_type=otype, is_active=True, base_asset_address="0x" + "11" * 20, base_asset_sub_id=1,
                      tick_size=Decimal("0.1"), amount_step=Decimal("0.1"), minimum_amount=Decimal("0.1"))


def ticker(i, ask=40.0, bid=38.0, delta=-0.3):
    return Ticker(instrument=i, best_bid=bid, best_ask=ask, bid_size=10, ask_size=10, mark_price=(ask + bid) / 2,
                  index_price=2500, delta=delta, iv=0.6, taker_fee_rate=0.0003)


def lp(price=2500):
    lo, hi = META.range_ticks(price, 10, 10)
    return LPPosition(liquidity=4 * 10**15, tick_lower=lo, tick_upper=hi, meta=META)


INSTRUMENTS = [inst(k, d) for d in (1, 5, 14, 30) for k in (2000, 2200, 2250, 2300, 2500, 2800)] + [inst(2250, 14, "C")]


def test_compute_target_protective_put():
    s = HedgeSettings()
    t = compute_target(lp(), 2500, s)
    assert t.contracts == pytest.approx(t.eth_at_lower)
    assert 2240 < t.strike_target < 2260
    s.coverage_pct = 50
    assert compute_target(lp(), 2500, s).contracts == pytest.approx(0.5 * t.eth_at_lower)


def test_compute_target_delta_neutral():
    s = HedgeSettings(mode="delta_neutral")
    t = compute_target(lp(), 2500, s, put_delta=-0.25)
    assert t.contracts == pytest.approx(t.lp_delta_eth / 0.25)
    assert t.contracts <= 2 * t.eth_at_lower


def test_select_instrument_prefers_target_expiry_and_strike():
    s = HedgeSettings(target_days_to_expiry=14, min_days_to_expiry=3, roll_days_before_expiry=2)
    i = select_instrument(INSTRUMENTS, 2240, s, now=NOW)
    assert i.option_type == "P" and i.strike == 2250 and abs((i.expiry - NOW) / DAY - 14) < 0.01
    # too-close expiries are excluded even if nearest to a tiny target
    s.target_days_to_expiry = 1
    i = select_instrument(INSTRUMENTS, 2240, s, now=NOW)
    assert (i.expiry - NOW) / DAY >= 3


def test_decide_buys_when_unhedged_and_respects_budget():
    s = HedgeSettings(max_premium_pct=100)
    l = lp()
    target, action = decide(l, 2500, l.value_usd(2500), [], INSTRUMENTS, {}, s)
    assert action.kind == "need_ticker"
    tk = ticker(next(i for i in INSTRUMENTS if i.name == action.instrument_name))
    target, action = decide(l, 2500, l.value_usd(2500), [], INSTRUMENTS, {tk.instrument.name: tk}, s)
    assert action.kind == "buy" and action.amount == pytest.approx(target.contracts)
    # tight premium budget scales the order down and warns
    s.max_premium_pct = 0.01
    target, action = decide(l, 2500, l.value_usd(2500), [], INSTRUMENTS, {tk.instrument.name: tk}, s)
    assert action.kind in ("buy", "none") and action.warnings


def test_decide_within_tolerance_and_sticky_instrument():
    s = HedgeSettings()
    l = lp()
    t = compute_target(l, 2500, s)
    held = OptionPosition("ETH-D14-2300-P", t.contracts * 0.95, 40, 39, -0.3, 2300, int(NOW + 14 * DAY), "P")
    assert pick_existing_hedge([held], t.strike_target, s).instrument_name == "ETH-D14-2300-P"
    tk = ticker(next(i for i in INSTRUMENTS if i.name == "ETH-D14-2300-P"))
    _, action = decide(l, 2500, l.value_usd(2500), [held], INSTRUMENTS, {"ETH-D14-2300-P": tk}, s)
    assert action.kind == "none" and action.instrument_name == "ETH-D14-2300-P"


def test_decide_rolls_stale_hedge():
    s = HedgeSettings(roll_days_before_expiry=2)
    l = lp()
    t = compute_target(l, 2500, s)
    stale = OptionPosition("ETH-D1-2250-P", t.contracts, 40, 10, -0.5, 2250, int(NOW + 1 * DAY), "P")
    _, action = decide(l, 2500, l.value_usd(2500), [stale], INSTRUMENTS, {}, s)
    tk = ticker(next(i for i in INSTRUMENTS if i.name == action.instrument_name))
    assert (tk.instrument.expiry - NOW) / DAY > 2
    _, action = decide(l, 2500, l.value_usd(2500), [stale], INSTRUMENTS, {tk.instrument.name: tk}, s)
    assert action.kind == "roll" and action.close_positions == [stale] and action.amount == pytest.approx(t.contracts)


def test_decide_sells_when_overhedged_and_closes_without_lp():
    s = HedgeSettings()
    l = lp()
    t = compute_target(l, 2500, s)
    big = OptionPosition("ETH-D14-2250-P", t.contracts * 2, 40, 39, -0.3, 2250, int(NOW + 14 * DAY), "P")
    tk = ticker(next(i for i in INSTRUMENTS if i.name == "ETH-D14-2250-P"))
    _, action = decide(l, 2500, l.value_usd(2500), [big], INSTRUMENTS, {"ETH-D14-2250-P": tk}, s)
    assert action.kind == "sell" and action.amount == pytest.approx(t.contracts)
    _, action = decide(None, 2500, 0, [big], INSTRUMENTS, {}, s)
    assert action.kind == "sell" and action.close_positions == [big]


def test_scenario_table_hedge_caps_downside():
    l = lp()
    t = compute_target(l, 2500, HedgeSettings())
    put = OptionPosition("ETH-D14-2250-P", t.contracts, 40, 40, -0.3, 2250, int(NOW + 14 * DAY), "P")
    rows = scenario_table(l, 2500, [put])
    r40 = next(r for r in rows if r["move_pct"] == -40)
    r20 = next(r for r in rows if r["move_pct"] == -20)
    # below the strike the combined P&L stops getting worse
    assert r40["lp_pnl"] < r20["lp_pnl"]
    assert abs(r40["total_pnl"] - r20["total_pnl"]) < abs(r40["lp_pnl"] - r20["lp_pnl"]) * 0.05
    assert next(r for r in rows if r["move_pct"] == 0)["lp_pnl"] == pytest.approx(0)
