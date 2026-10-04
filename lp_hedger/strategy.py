"""Hedge policy: how many puts, which strike, which expiry, when to roll.

Pure functions over already-fetched data so the policy is unit-testable
without touching the chain or the Derive API.

The core idea
-------------
A concentrated ETH/USD LP range [Pa, Pb] behaves like a *short put* on ETH:
as price falls toward Pa the position converts into ETH, and below Pa it is
100% ETH with linear downside. Buying puts struck near Pa, sized to the ETH
the range holds at Pa, caps that downside. The put premium is the cost of
insurance, which the LP fees should pay for.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .config import HedgeSettings
from .derive import Instrument, OptionPosition, Ticker
from .uniswap_math import LPPosition


@dataclass
class HedgeTarget:
    contracts: float            # desired long puts
    strike_target: float        # ideal strike in USD
    eth_at_lower: float         # ETH exposure once price is at range low
    lp_delta_eth: float         # current dV/dP of the LP in ETH
    reason: str = ""


@dataclass
class HedgeAction:
    kind: str                   # "none" | "buy" | "sell" | "roll"
    instrument_name: Optional[str] = None
    amount: float = 0.0
    close_positions: list[OptionPosition] = field(default_factory=list)
    note: str = ""
    warnings: list[str] = field(default_factory=list)


def compute_target(lp: LPPosition, price: float, s: HedgeSettings, put_delta: Optional[float] = None) -> HedgeTarget:
    eth_low = lp.eth_at_lower_bound()
    lp_delta = lp.delta_eth(price)
    strike = lp.price_low * (1 + s.strike_offset_pct / 100.0)
    strike = min(strike, price * 0.995)  # never target an in-the-money strike by accident
    cov = s.coverage_pct / 100.0
    if s.mode == "delta_neutral":
        if put_delta is None or abs(put_delta) < 0.02:
            contracts = cov * eth_low
            reason = "delta_neutral fallback: no put delta, using range exposure"
        else:
            contracts = cov * lp_delta / abs(put_delta)
            contracts = min(contracts, 2.0 * eth_low)   # sanity cap on far-OTM gearing
            reason = f"delta_neutral: cover {lp_delta:.4f} ETH delta with |Δ|={abs(put_delta):.2f} puts"
    else:
        contracts = cov * eth_low
        reason = f"protective_put: cover {cov:.0%} of {eth_low:.4f} ETH held at range low"
    return HedgeTarget(contracts=max(0.0, contracts), strike_target=strike, eth_at_lower=eth_low,
                       lp_delta_eth=lp_delta, reason=reason)


def select_instrument(instruments: list[Instrument], strike_target: float, s: HedgeSettings,
                      now: Optional[float] = None) -> Optional[Instrument]:
    """Pick the put whose expiry is closest to target_days and strike closest to target."""
    now = now or time.time()
    puts = [i for i in instruments if i.option_type == "P" and i.is_active and i.expiry > now]
    if not puts:
        return None
    min_dte = max(s.min_days_to_expiry, s.roll_days_before_expiry + 1)
    eligible = [i for i in puts if (i.expiry - now) / 86400 >= min_dte]
    if not eligible:
        return None
    expiries = sorted({i.expiry for i in eligible})
    best_expiry = min(expiries, key=lambda e: abs((e - now) / 86400 - s.target_days_to_expiry))
    same_expiry = [i for i in eligible if i.expiry == best_expiry]
    return min(same_expiry, key=lambda i: abs(i.strike - strike_target))


def pick_existing_hedge(positions: list[OptionPosition], strike_target: float, s: HedgeSettings) -> Optional[OptionPosition]:
    """Keep using an already-held put if it is still reasonably close to what we want."""
    longs = [p for p in positions if p.option_type == "P" and p.amount > 0]
    if not longs:
        return None
    ok = [p for p in longs
          if p.days_to_expiry >= s.roll_days_before_expiry
          and abs(p.strike - strike_target) / max(strike_target, 1e-9) <= s.strike_drift_pct / 100.0]
    if not ok:
        return None
    return max(ok, key=lambda p: p.amount)


def decide(lp: Optional[LPPosition], price: float, lp_value_usd: float, positions: list[OptionPosition],
           instruments: list[Instrument], tickers: dict[str, Ticker], s: HedgeSettings) -> tuple[Optional[HedgeTarget], HedgeAction]:
    """Full decision for one engine tick.

    ``tickers`` must contain the ticker for any instrument we may trade; the
    engine fetches it after a first call with an empty dict returns the name
    in ``HedgeAction.instrument_name`` with kind "need_ticker".
    """
    if not s.enabled:
        return None, HedgeAction("none", note="hedging disabled")
    stale = [p for p in positions if p.option_type == "P" and p.amount > 0
             and p.days_to_expiry < s.roll_days_before_expiry]
    if lp is None or lp.liquidity == 0:
        longs = [p for p in positions if p.option_type == "P" and p.amount > 0]
        if longs and s.allow_reduce:
            return None, HedgeAction("sell", close_positions=longs, note="no LP position: closing hedges")
        return None, HedgeAction("none", note="no LP position")

    target0 = compute_target(lp, price, s)
    held = pick_existing_hedge(positions, target0.strike_target, s)
    if held is not None:
        name = held.instrument_name
        inst = next((i for i in instruments if i.name == name), None)
    else:
        inst = select_instrument(instruments, target0.strike_target, s)
        name = inst.name if inst else None
    if inst is None:
        return target0, HedgeAction("none", note="no eligible put instrument on Derive",
                                    warnings=["No put instrument matched the expiry/strike filters"])
    tk = tickers.get(name)
    if tk is None:
        return target0, HedgeAction("need_ticker", instrument_name=name)

    target = compute_target(lp, price, s, put_delta=tk.delta)
    held_amt = held.amount if held else 0.0
    # puts in other instruments that are not stale still count toward coverage
    other = [p for p in positions if p.option_type == "P" and p.amount > 0
             and p.instrument_name != name and p not in stale]
    other_amt = sum(p.amount for p in other)
    diff = target.contracts - held_amt - other_amt
    tol = max(float(inst.minimum_amount), target.contracts * s.rehedge_tolerance_pct / 100.0)
    warnings: list[str] = []

    if stale:
        # Roll: close the stale legs; the buy below replaces them.
        diff = target.contracts - held_amt - other_amt  # stale excluded already
        kind = "roll"
    else:
        kind = "none"

    if diff > tol:
        amount = diff
        # premium guard
        premium = amount * (tk.best_ask or tk.mark_price)
        budget = lp_value_usd * s.max_premium_pct / 100.0
        if budget > 0 and premium > budget:
            scaled = budget / max(tk.best_ask or tk.mark_price, 1e-9)
            warnings.append(f"Premium ${premium:,.0f} exceeds budget ${budget:,.0f}; "
                            f"buying {scaled:.3f} instead of {amount:.3f}")
            amount = scaled
        if amount < float(inst.minimum_amount):
            return target, HedgeAction("none" if kind == "none" else "roll", close_positions=stale,
                                       note="buy skipped: below min size after budget", warnings=warnings)
        return target, HedgeAction("buy" if kind == "none" else "roll", instrument_name=name, amount=amount,
                                   close_positions=stale, note=target.reason, warnings=warnings)
    if diff < -tol and s.allow_reduce and held_amt > 0:
        amount = min(-diff, held_amt)
        return target, HedgeAction("sell", instrument_name=name, amount=amount, close_positions=stale,
                                   note=f"over-hedged by {-diff:.3f}", warnings=warnings)
    if stale:
        return target, HedgeAction("roll", instrument_name=name, amount=0.0, close_positions=stale,
                                   note="closing stale hedge legs", warnings=warnings)
    return target, HedgeAction("none", instrument_name=name, note=f"hedge within tolerance ({held_amt + other_amt:.3f}/{target.contracts:.3f})")


def scenario_table(lp: Optional[LPPosition], price: float, positions: list[OptionPosition],
                   moves=(-40, -30, -20, -15, -10, -5, 0, 5, 10, 15, 20, 30, 40)) -> list[dict]:
    """P&L vs today at expiry-style payoffs for a range of ETH moves."""
    rows = []
    base_lp = lp.value_usd(price) if lp else 0.0
    base_hedge = sum(p.amount * p.mark_price for p in positions)
    for m in moves:
        p2 = price * (1 + m / 100.0)
        lp_v = lp.value_usd(p2) if lp else 0.0
        hedge_v = 0.0
        for pos in positions:
            intrinsic = max(pos.strike - p2, 0.0) if pos.option_type == "P" else max(p2 - pos.strike, 0.0)
            hedge_v += pos.amount * intrinsic
        hodl = 0.0
        if lp:
            eth0, usd0 = lp.holdings(price)
            hodl = eth0 * p2 + usd0 - base_lp
        rows.append({
            "move_pct": m, "price": p2,
            "lp_pnl": lp_v - base_lp,
            "hedge_pnl": hedge_v - base_hedge,
            "total_pnl": (lp_v - base_lp) + (hedge_v - base_hedge),
            "hodl_pnl": hodl,
        })
    return rows
