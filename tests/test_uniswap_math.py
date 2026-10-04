from decimal import Decimal

import pytest

from lp_hedger.uniswap_math import (
    LPPosition, PoolMeta, amounts_for_liquidity, liquidity_for_amounts, plan_mint, sqrt_ratio_at_tick,
    sqrt_price_x96_at_tick, Q96,
)

META0 = PoolMeta(eth_is_token0=True, usd_decimals=6, tick_spacing=10)   # Arbitrum / Base layout
META1 = PoolMeta(eth_is_token0=False, usd_decimals=6, tick_spacing=10)  # hypothetical inverted pool


@pytest.mark.parametrize("meta", [META0, META1])
@pytest.mark.parametrize("price", [1500.0, 2500.0, 4321.5])
def test_price_tick_roundtrip(meta, price):
    tick = meta.tick_from_price(price)
    p = meta.price_from_tick(tick)
    assert abs(p - price) / price < 2e-4   # one tick is 1bp


def test_sqrt_x96_consistency():
    t = -201000  # typical ETH/USDC tick on arbitrum (ETH token0)
    assert abs(sqrt_price_x96_at_tick(t) / Q96 - float(sqrt_ratio_at_tick(t))) < 1e-9
    # tick 0 -> raw price 1 -> for ETH token0 that's 1e12 USD per ETH
    assert abs(META0.price_from_sqrt_x96(Q96) - 1e12) < 1


def test_range_ticks_aligned_and_ordered():
    lo, hi = META0.range_ticks(2500, 10, 10)
    assert lo % 10 == 0 and hi % 10 == 0 and lo < hi
    plo, phi = META0.price_bounds(lo, hi)
    assert 2240 < plo < 2260 and 2740 < phi < 2760


def test_range_ticks_inverted_pool():
    lo, hi = META1.range_ticks(2500, 10, 10)
    plo, phi = META1.price_bounds(lo, hi)
    assert 2240 < plo < 2260 and 2740 < phi < 2760


def test_amounts_liquidity_roundtrip():
    sp, sa, sb = META0.raw_sqrt_from_price(2500), META0.raw_sqrt_from_price(2250), META0.raw_sqrt_from_price(2750)
    L = 10**15
    a0, a1 = amounts_for_liquidity(L, sp, sa, sb)
    assert a0 > 0 and a1 > 0
    L2 = liquidity_for_amounts(a0, a1, sp, sa, sb)
    assert abs(L2 - L) / L < 1e-9
    # all token0 below range, all token1 above
    assert amounts_for_liquidity(L, sa / 2, sa, sb)[1] == 0
    assert amounts_for_liquidity(L, sb * 2, sa, sb)[0] == 0


def test_position_holdings_and_exposure():
    lo, hi = META0.range_ticks(2500, 10, 10)
    pos = LPPosition(liquidity=10**15, tick_lower=lo, tick_upper=hi, meta=META0)
    eth, usd = pos.holdings(2500)
    assert eth > 0 and usd > 0
    # value is roughly balanced at the centre of a symmetric range
    assert 0.8 < (eth * 2500) / usd < 1.25
    eth_low = pos.eth_at_lower_bound()
    assert eth_low > eth                         # more ETH as price falls
    assert pos.holdings(1000)[0] == pytest.approx(eth_low, rel=1e-6)   # below range: constant
    assert pos.holdings(5000)[0] == 0            # above range: no ETH
    assert pos.in_range(2500) and not pos.in_range(2000)
    # value is monotonic in price
    vals = [pos.value_usd(p) for p in (1500, 2000, 2250, 2500, 2750, 3000)]
    assert vals == sorted(vals)
    assert pos.value_usd(3000) == pytest.approx(pos.value_usd(2800))  # flat above range


def test_plan_mint_caps_by_wallet():
    lo, hi = META0.range_ticks(2500, 10, 10)
    full = plan_mint(META0, 2500, lo, hi, eth_available=10, usd_available=100000, target_usd=1000)
    assert full["value_usd"] == pytest.approx(1000, rel=1e-6)
    assert full["short_eth"] == 0 and full["short_usd"] == 0
    capped = plan_mint(META0, 2500, lo, hi, eth_available=0.05, usd_available=100000, target_usd=1000)
    assert capped["value_usd"] < 1000 and capped["eth"] == pytest.approx(0.05, rel=1e-6)
    assert capped["short_eth"] > 0
