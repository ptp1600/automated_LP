import time
from decimal import Decimal

import pytest

from lp_hedger.derive import Instrument, Ticker
from lp_hedger.paper import (
    PaperHedge, PaperLP, close_cost_usd, fill_price, gas_cost_usd, open_paper_lp, option_fee_usd, plan_paper_lp,
    projection, swap_cost_usd,
)
from lp_hedger.pool import Q128, PoolState
from lp_hedger.uniswap_math import PoolMeta

META = PoolMeta(eth_is_token0=True, usd_decimals=6, tick_spacing=10)
META_INV = PoolMeta(eth_is_token0=False, usd_decimals=6, tick_spacing=10)
NOW = 1_800_000_000.0
DAY = 86400


def state(price, fg0=0, fg1=0, liq=10**18, ts=NOW, block=1):
    return PoolState(block=block, ts=ts, sqrt_price_x96=0, tick=META.tick_from_price(price), price=price,
                     liquidity=liq, fg0=fg0, fg1=fg1)


def inst(strike=2250, days=14, otype="P"):
    return Instrument(name=f"ETH-TEST-{strike}-{otype}", strike=strike, expiry=int(NOW + days * DAY), option_type=otype,
                      is_active=True, base_asset_address="0x" + "11" * 20, base_asset_sub_id=1,
                      tick_size=Decimal("0.1"), amount_step=Decimal("0.01"), minimum_amount=Decimal("0.1"),
                      taker_fee_rate=0.0003, base_fee=0.5)


def ticker(i=None, bid=38.0, ask=40.0, bid_size=5.0, ask_size=5.0, mark=39.0, index=2500.0):
    return Ticker(instrument=i or inst(), best_bid=bid, best_ask=ask, bid_size=bid_size, ask_size=ask_size,
                  mark_price=mark, index_price=index, delta=-0.3, iv=0.6, taker_fee_rate=0.0003)


# ---- costs -------------------------------------------------------------------------------------

def test_swap_cost_fee_and_impact_scale_with_size_and_liquidity():
    small = swap_cost_usd(5000, 2500, 10**18, META, 0.0005, sell_eth=False)
    assert small["fee_usd"] == pytest.approx(2.5)
    assert 0 < small["impact_usd"] < small["fee_usd"]
    big = swap_cost_usd(500_000, 2500, 10**18, META, 0.0005, sell_eth=False)
    assert big["impact_pct"] == pytest.approx(small["impact_pct"] * 100, rel=1e-6)
    thin = swap_cost_usd(5000, 2500, 10**16, META, 0.0005, sell_eth=False)
    assert thin["impact_usd"] == pytest.approx(small["impact_usd"] * 100, rel=1e-6)
    # ETH side and inverted token order give the same order of magnitude
    inv = swap_cost_usd(5000, 2500, 10**18, META_INV, 0.0005, sell_eth=True)
    assert inv["impact_usd"] == pytest.approx(small["impact_usd"], rel=0.05)
    assert swap_cost_usd(5000, 2500, 0, META, 0.0005, False)["impact_usd"] == 0
    assert gas_cost_usd(10**9, 500_000, 2500) == pytest.approx(1.25)


# ---- paper LP -----------------------------------------------------------------------------------

def test_open_paper_lp_sizes_and_charges_entry():
    st = state(2500)
    lp = open_paper_lp("base", "ETH/USDC 0.05%", st, META, 0.0005, 10_000, 10, 10, True, 10**8, 550_000, None)
    assert lp.entry_value_usd == pytest.approx(10_000, rel=1e-3)
    assert 0.4 * 10_000 < lp.entry_eth * 2500 < 0.6 * 10_000       # roughly half in ETH for a symmetric range
    assert lp.entry_cost_usd > 0 and lp.entry_cost_detail["swap_fee_usd"] == pytest.approx(lp.entry_eth * 2500 * 0.0005)
    snap = lp.snapshot(2500, META)
    assert snap["in_range"] and snap["pnl_usd"] == pytest.approx(-lp.entry_cost_usd, abs=0.05)
    assert snap["price_low"] < 2500 < snap["price_high"]
    free = open_paper_lp("base", "x", st, META, 0.0005, 10_000, 10, 10, False, 10**8, 550_000, None)
    assert free.entry_cost_usd == 0


def test_accrue_gated_credits_only_in_range():
    st0 = state(2500)
    lp = open_paper_lp("base", "p", st0, META, 0.0005, 10_000, 10, 10, False, 0, 0, None)
    L = lp.liquidity
    growth1 = 5 * 10**6 * Q128          # 5 USDC per unit L on token1 (USDC)
    st1 = state(2510, fg0=0, fg1=growth1, ts=NOW + 60, block=2)
    acc = lp.accrue(st1, None, META)
    assert acc["method"] == "gated" and acc["in_range"]
    dilution = 10**18 / (10**18 + L)
    assert lp.fees_usd == pytest.approx(5 * L * dilution / 1, rel=1e-9) or lp.fees_usd == pytest.approx(5 * 10**6 * L / 10**6 * dilution, rel=1e-9)
    # price far above the range: no more credit
    st2 = state(3500, fg0=0, fg1=2 * growth1, ts=NOW + 120, block=3)
    before = lp.fees_usd
    acc2 = lp.accrue(st2, None, META)
    assert not acc2["in_range"] and lp.fees_usd == pytest.approx(before + (5 * 10**6 * L / 10**6) * 0.5 * (10**18 / (10**18 + L)), rel=1e-9)
    st3 = state(3600, fg0=0, fg1=3 * growth1, ts=NOW + 180, block=4)
    lp.accrue(st3, None, META)
    assert lp.fees_usd == pytest.approx(before + (5 * 10**6 * L / 10**6) * 0.5 * (10**18 / (10**18 + L)), rel=1e-9)
    assert lp.gated_ticks == 3 and lp.seconds_tracked == 180
    assert lp.snapshot(3600, META)["time_in_range_pct"] == pytest.approx((60 + 30) / 180 * 100)


def test_accrue_exact_uses_fee_growth_inside_when_sane():
    st0 = state(2500)
    lp = open_paper_lp("base", "p", st0, META, 0.0005, 10_000, 10, 10, False, 0, 0, (0, 0))
    g = 4 * 10**6 * Q128
    st1 = state(2500, fg1=g, ts=NOW + 60)
    acc = lp.accrue(st1, (0, g // 2), META)      # range got half of the pool's growth
    assert acc["method"] == "exact"
    dil = 10**18 / (10**18 + lp.liquidity)
    assert lp.fees_usd == pytest.approx(2 * lp.liquidity * dil, rel=1e-9)
    # an impossible inside delta (more than global) falls back to gated accounting
    st2 = state(2500, fg1=g + 10**6 * Q128, ts=NOW + 120)
    acc2 = lp.accrue(st2, (0, g // 2 + 10 * 10**6 * Q128), META)
    assert acc2["method"] == "gated"


def test_close_cost_and_roundtrip():
    st = state(2500)
    lp = open_paper_lp("base", "p", st, META, 0.0005, 10_000, 10, 10, True, 10**8, 550_000, None)
    cc = close_cost_usd(lp, st, META, 0.0005, True, 10**8, 350_000)
    assert cc["total_usd"] > 0 and cc["swap_fee_usd"] == pytest.approx(lp.entry_eth * 2500 * 0.0005, rel=1e-6)
    assert close_cost_usd(lp, st, META, 0.0005, False, 10**8, 350_000)["total_usd"] == 0
    again = PaperLP.from_dict(lp.to_dict())
    assert again.snapshot(2500, META)["value_usd"] == pytest.approx(lp.snapshot(2500, META)["value_usd"])


def test_plan_paper_lp_bounds():
    p = plan_paper_lp(META, 2500, 10_000, 10, 20)
    assert p["price_low"] == pytest.approx(2250, rel=0.01) and p["price_high"] == pytest.approx(3000, rel=0.01)
    assert p["value_usd"] == pytest.approx(10_000, rel=1e-3)


# ---- paper hedge -------------------------------------------------------------------------------

def test_fill_price_walks_beyond_top_of_book():
    tk = ticker(ask=40, ask_size=1.0, bid=38, bid_size=1.0)
    px, w = fill_price(tk, "buy", 0.5, 5.0)
    assert px == 40 and not w
    px, w = fill_price(tk, "buy", 2.0, 5.0)
    assert px == pytest.approx((40 * 1 + 42 * 1) / 2) and w
    px, w = fill_price(tk, "sell", 2.0, 5.0)
    assert px == pytest.approx((38 * 1 + 36.1 * 1) / 2)
    px, w = fill_price(ticker(ask=0, mark=39), "buy", 1, 5.0)
    assert px == pytest.approx(39 * 1.05) and w


def test_option_fee_rate_cap_and_base():
    tk = ticker(index=2500)
    assert option_fee_usd(tk, 2.0, 40.0) == pytest.approx(0.0003 * 2500 * 2 + 0.5)
    cheap = option_fee_usd(tk, 2.0, 1.0)       # 12.5% cap of a $1 option beats 0.75/contract
    assert cheap == pytest.approx(0.125 * 1.0 * 2 + 0.5)


def test_paper_hedge_buy_mark_sell_settle():
    h = PaperHedge()
    tk = ticker(ask=40, ask_size=10, bid=38, bid_size=10, mark=39)
    tr = h.buy(tk, 1.234, 3.0, ts=NOW)
    assert tr["amount"] == pytest.approx(1.23) and tr["price"] == 40
    assert h.premium_paid_usd == pytest.approx(1.23 * 40) and h.fees_paid_usd == pytest.approx(0.0003 * 2500 * 1.23 + 0.5)
    assert h.value_usd() == pytest.approx(1.23 * 39)
    assert h.pnl_usd() == pytest.approx(1.23 * 39 - 1.23 * 40 - h.fees_paid_usd)
    pos = h.positions()
    assert len(pos) == 1 and pos[0].amount == pytest.approx(1.23) and pos[0].strike == 2250
    h.mark_to_market({tk.instrument.name: ticker(mark=60, bid=58, ask=62)})
    assert h.value_usd() == pytest.approx(1.23 * 60) and h.liquidation_value_usd() == pytest.approx(1.23 * 58)
    tr2 = h.sell(ticker(bid=58, ask=62, mark=60, bid_size=10), 0.5, 3.0, ts=NOW + 10)
    assert tr2["price"] == 58 and h.legs[0].amount == pytest.approx(0.73)
    assert h.premium_received_usd == pytest.approx(0.5 * 58)
    # expire in the money: payout = (2250 - 2000) x 0.73
    out = h.settle_expired(2000.0, now=NOW + 15 * DAY)
    assert len(out) == 1 and out[0]["payout_usd"] == pytest.approx(250 * 0.73)
    assert h.legs == [] and h.settled_payout_usd == pytest.approx(250 * 0.73)
    assert h.value_usd() == 0
    s = h.summary()
    assert s["held_contracts"] == 0 and len(s["trades"]) == 3
    again = PaperHedge.from_dict(h.to_dict())
    assert again.net_cost_usd == pytest.approx(h.net_cost_usd)


def test_settle_out_of_the_money_pays_nothing():
    h = PaperHedge()
    h.buy(ticker(), 1.0, 3.0, ts=NOW)
    out = h.settle_expired(2600.0, now=NOW + 15 * DAY)
    assert out[0]["payout_usd"] == 0 and out[0]["pnl_usd"] == pytest.approx(-40 - h.fees_paid_usd)


# ---- projection ----------------------------------------------------------------------------------

def test_projection_numbers():
    rs = {"fees_per_day_usd": 10.0, "days_covered": 5.0, "time_in_range_pct": 90.0, "fees_usd": 50.0}
    ps = {"avg_liquidity": 99 * 10**15}
    q = {"instrument": "ETH-X-2250-P", "contracts": 2.0, "price": 30.0, "fee_usd": 2.0, "days_to_expiry": 14.0}
    p = projection(rs, ps, 10**15, 10_000, 5.0, q, 14, 2)
    assert p["fee_apr_pct"] == pytest.approx(10 * 365 / 10_000 * 100)
    assert p["pool_share_pct"] == pytest.approx(1.0)
    assert p["hedge"]["premium_usd"] == 60 and p["hedge"]["held_days"] == 12
    assert p["hedge"]["cost_per_day_usd"] == pytest.approx(62 / 12)
    assert p["hedge"]["fees_cover_hedge_pct"] == pytest.approx(10 / (62 / 12) * 100)
    assert p["net_carry_per_day_usd"] == pytest.approx(10 - 62 / 12)
    assert p["horizon"]["net_usd"] == pytest.approx(14 * 10 - 14 * 62 / 12 - 5)
    unhedged = projection(rs, ps, 10**15, 10_000, 5.0, None, 14, 2)
    assert unhedged["hedge"] is None and unhedged["horizon"]["net_usd"] == pytest.approx(135)
