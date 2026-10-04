"""Pure Uniswap v3 math (no network).

Conventions
-----------
* ``price`` always means **USD per 1 ETH** (human units).
* ``raw`` sqrt prices are the pool's token1/token0 ratio in raw token units,
  i.e. what ``sqrtPriceX96`` encodes. ``PoolMeta`` converts between the two.
* Liquidity ``L`` is the pool's uint128 liquidity number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, getcontext

getcontext().prec = 60

Q96 = 2**96
MIN_TICK = -887272
MAX_TICK = 887272
MAX_UINT128 = 2**128 - 1


@dataclass(frozen=True)
class PoolMeta:
    """How the pool's token0/token1 map onto ETH and USD."""

    eth_is_token0: bool
    eth_decimals: int = 18
    usd_decimals: int = 6
    tick_spacing: int = 10

    # ---- price <-> sqrt ratio -------------------------------------------
    def raw_sqrt_from_price(self, price: float) -> Decimal:
        """USD/ETH -> sqrt(token1/token0) in raw units."""
        p = Decimal(str(price))
        if self.eth_is_token0:
            raw = p * Decimal(10) ** (self.usd_decimals - self.eth_decimals)
        else:
            raw = (Decimal(1) / p) * Decimal(10) ** (self.eth_decimals - self.usd_decimals)
        return raw.sqrt()

    def price_from_raw_sqrt(self, raw_sqrt: Decimal) -> float:
        raw = raw_sqrt * raw_sqrt
        if self.eth_is_token0:
            return float(raw * Decimal(10) ** (self.eth_decimals - self.usd_decimals))
        return float((Decimal(1) / raw) * Decimal(10) ** (self.eth_decimals - self.usd_decimals))

    def price_from_sqrt_x96(self, sqrt_price_x96: int) -> float:
        return self.price_from_raw_sqrt(Decimal(sqrt_price_x96) / Decimal(Q96))

    def tick_from_price(self, price: float) -> int:
        raw_sqrt = self.raw_sqrt_from_price(price)
        return int(math.floor(2 * math.log(float(raw_sqrt)) / math.log(1.0001)))

    def price_from_tick(self, tick: int) -> float:
        return self.price_from_raw_sqrt(sqrt_ratio_at_tick(tick))

    def align_tick(self, tick: int, up: bool = False) -> int:
        s = self.tick_spacing
        q = tick // s
        if up and tick % s != 0:
            q += 1
        return max(MIN_TICK // s * s, min(MAX_TICK // s * s, q * s))

    def range_ticks(self, price: float, down_pct: float, up_pct: float) -> tuple[int, int]:
        """Tick range for [price*(1-down), price*(1+up)], aligned to spacing.

        Returned as (tick_lower, tick_upper) in pool tick space (which is
        inverted relative to ETH price when ETH is token1).
        """
        lo_p = price * (1 - down_pct / 100.0)
        hi_p = price * (1 + up_pct / 100.0)
        t1 = self.tick_from_price(lo_p)
        t2 = self.tick_from_price(hi_p)
        lo, hi = sorted((t1, t2))
        lo = self.align_tick(lo)
        hi = self.align_tick(hi, up=True)
        if hi <= lo:
            hi = lo + self.tick_spacing
        return lo, hi

    def price_bounds(self, tick_lower: int, tick_upper: int) -> tuple[float, float]:
        """ETH price bounds (low, high) for a tick range."""
        a = self.price_from_tick(tick_lower)
        b = self.price_from_tick(tick_upper)
        return (min(a, b), max(a, b))

    # ---- raw amounts <-> human --------------------------------------------
    def to_human(self, amount0: int, amount1: int) -> tuple[float, float]:
        """raw (amount0, amount1) -> (eth, usd)."""
        if self.eth_is_token0:
            return amount0 / 10**self.eth_decimals, amount1 / 10**self.usd_decimals
        return amount1 / 10**self.eth_decimals, amount0 / 10**self.usd_decimals

    def to_raw(self, eth: float, usd: float) -> tuple[int, int]:
        e = int(Decimal(str(eth)) * Decimal(10) ** self.eth_decimals)
        u = int(Decimal(str(usd)) * Decimal(10) ** self.usd_decimals)
        return (e, u) if self.eth_is_token0 else (u, e)


def sqrt_ratio_at_tick(tick: int) -> Decimal:
    """sqrt(1.0001^tick) as a Decimal (not X96 scaled)."""
    return Decimal("1.0001") ** (Decimal(tick) / Decimal(2))


def sqrt_price_x96_at_tick(tick: int) -> int:
    return int(sqrt_ratio_at_tick(tick) * Q96)


# ---- liquidity math -----------------------------------------------------------

def amounts_for_liquidity(L: int, sqrt_p: Decimal, sqrt_a: Decimal, sqrt_b: Decimal) -> tuple[int, int]:
    """Raw (amount0, amount1) held by liquidity L at sqrt price p in [a, b]."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    Ld = Decimal(L)
    if sqrt_p <= sqrt_a:
        a0 = Ld * (Decimal(1) / sqrt_a - Decimal(1) / sqrt_b)
        a1 = Decimal(0)
    elif sqrt_p >= sqrt_b:
        a0 = Decimal(0)
        a1 = Ld * (sqrt_b - sqrt_a)
    else:
        a0 = Ld * (Decimal(1) / sqrt_p - Decimal(1) / sqrt_b)
        a1 = Ld * (sqrt_p - sqrt_a)
    return int(a0), int(a1)


def liquidity_for_amounts(amount0: int, amount1: int, sqrt_p: Decimal, sqrt_a: Decimal, sqrt_b: Decimal) -> int:
    """Max liquidity mintable from (amount0, amount1) at sqrt price p in [a, b]."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_p <= sqrt_a:
        return int(Decimal(amount0) / (Decimal(1) / sqrt_a - Decimal(1) / sqrt_b))
    if sqrt_p >= sqrt_b:
        return int(Decimal(amount1) / (sqrt_b - sqrt_a))
    l0 = Decimal(amount0) / (Decimal(1) / sqrt_p - Decimal(1) / sqrt_b)
    l1 = Decimal(amount1) / (sqrt_p - sqrt_a)
    return int(min(l0, l1))


def max_token0_for_liquidity(L: int, sqrt_a: Decimal, sqrt_b: Decimal) -> int:
    """token0 held when price is at/below the lower bound (all in token0)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return int(Decimal(L) * (Decimal(1) / sqrt_a - Decimal(1) / sqrt_b))


def max_token1_for_liquidity(L: int, sqrt_a: Decimal, sqrt_b: Decimal) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return int(Decimal(L) * (sqrt_b - sqrt_a))


# ---- human-unit helpers used by the strategy -----------------------------------

@dataclass(frozen=True)
class LPPosition:
    """A position described in human units (ETH price space)."""

    liquidity: int
    tick_lower: int
    tick_upper: int
    meta: PoolMeta

    @property
    def price_low(self) -> float:
        return self.meta.price_bounds(self.tick_lower, self.tick_upper)[0]

    @property
    def price_high(self) -> float:
        return self.meta.price_bounds(self.tick_lower, self.tick_upper)[1]

    def holdings(self, price: float) -> tuple[float, float]:
        """(eth, usd) held at the given ETH price."""
        sp = self.meta.raw_sqrt_from_price(price)
        sa = sqrt_ratio_at_tick(self.tick_lower)
        sb = sqrt_ratio_at_tick(self.tick_upper)
        a0, a1 = amounts_for_liquidity(self.liquidity, sp, sa, sb)
        return self.meta.to_human(a0, a1)

    def value_usd(self, price: float) -> float:
        eth, usd = self.holdings(price)
        return eth * price + usd

    def eth_at_lower_bound(self) -> float:
        """ETH held once price has fallen to/below the range: the max downside exposure."""
        eth, _ = self.holdings(self.price_low * 0.999999)
        return eth

    def delta_eth(self, price: float) -> float:
        """dValue/dPrice in ETH units == ETH currently held (exact for v3 in range)."""
        eth, _ = self.holdings(price)
        return eth

    def in_range(self, price: float) -> bool:
        return self.price_low < price < self.price_high


def plan_mint(meta: PoolMeta, price: float, tick_lower: int, tick_upper: int,
              eth_available: float, usd_available: float, target_usd: float) -> dict:
    """Decide how much of each token to deposit for a target USD size.

    Returns amounts actually mintable given wallet balances, the implied
    liquidity, and what would be needed for the full target (so the UI can
    tell the user what to top up).
    """
    sp = meta.raw_sqrt_from_price(price)
    sa = sqrt_ratio_at_tick(tick_lower)
    sb = sqrt_ratio_at_tick(tick_upper)
    # Amounts per unit liquidity at current price
    a0_unit, a1_unit = amounts_for_liquidity(10**18, sp, sa, sb)
    eth_unit, usd_unit = meta.to_human(a0_unit, a1_unit)
    value_unit = eth_unit * price + usd_unit
    if value_unit <= 0:
        raise ValueError("Range does not contain liquidity value at this price")
    L_target = Decimal(target_usd) / Decimal(value_unit) * Decimal(10**18)
    need_eth = float(L_target) * eth_unit / 1e18
    need_usd = float(L_target) * usd_unit / 1e18

    # Cap by wallet balances
    L = L_target
    if eth_unit > 0:
        L = min(L, Decimal(str(eth_available)) / Decimal(str(eth_unit)) * Decimal(10**18))
    if usd_unit > 0:
        L = min(L, Decimal(str(usd_available)) / Decimal(str(usd_unit)) * Decimal(10**18))
    L = max(L, Decimal(0))
    a0, a1 = amounts_for_liquidity(int(L), sp, sa, sb)
    eth_use, usd_use = meta.to_human(a0, a1)
    return {
        "liquidity": int(L),
        "amount0": a0,
        "amount1": a1,
        "eth": eth_use,
        "usd": usd_use,
        "value_usd": eth_use * price + usd_use,
        "need_eth": need_eth,
        "need_usd": need_usd,
        "short_eth": max(0.0, need_eth - eth_available),
        "short_usd": max(0.0, need_usd - usd_available),
    }
