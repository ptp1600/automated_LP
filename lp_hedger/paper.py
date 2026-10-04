"""Paper positions: a simulated Uniswap v3 LP and a simulated put hedge.

Nothing here touches a wallet. The LP earns exactly what the pool's
fee-growth accumulators say an LP of its size would have earned (diluted by
its own share of the pool), pays simulated entry costs, and is valued with
the real v3 math. The hedge is filled against Derive's live order book at
the price a taker would actually pay (ask plus fees, worse beyond the top of
book), marked each tick and settled at expiry.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Optional

from .derive import OptionPosition, Ticker
from .pool import Q128, U256, PoolState
from .uniswap_math import LPPosition, PoolMeta, plan_mint

OPTION_FEE_CAP = 0.125          # Derive caps the taker fee at 12.5% of the option price
MAX_IMPACT = 0.5


# ---- cost models -------------------------------------------------------------------------

def swap_cost_usd(value_usd: float, price: float, liquidity: int, meta: PoolMeta, fee_rate: float,
                  sell_eth: bool) -> dict:
    """Cost of swapping ``value_usd`` through the pool: LP fee plus price impact
    against the currently active liquidity (average execution = half the move)."""
    fee = value_usd * fee_rate
    impact_rel = 0.0
    if liquidity > 0 and value_usd > 0:
        sqrt_p = float(meta.raw_sqrt_from_price(price))
        if sell_eth:
            in_raw = value_usd / price * 10 ** meta.eth_decimals
            token_in_is_1 = not meta.eth_is_token0
        else:
            in_raw = value_usd * 10 ** meta.usd_decimals
            token_in_is_1 = meta.eth_is_token0
        if token_in_is_1:
            impact_rel = 2 * in_raw / (liquidity * sqrt_p)
        else:
            impact_rel = 2 * in_raw * sqrt_p / liquidity
        impact_rel = min(MAX_IMPACT, impact_rel)
    impact = value_usd * impact_rel / 2
    return {"fee_usd": fee, "impact_usd": impact, "impact_pct": impact_rel * 100, "total_usd": fee + impact}


def gas_cost_usd(gas_price_wei: int, units: int, eth_price: float) -> float:
    return gas_price_wei * units / 1e18 * eth_price


# ---- paper LP ------------------------------------------------------------------------------

@dataclass
class PaperLP:
    chain: str
    pool: str
    liquidity: int
    tick_lower: int
    tick_upper: int
    deploy_usd: float
    entry_ts: float
    entry_block: int
    entry_price: float
    entry_eth: float
    entry_usd: float
    entry_cost_usd: float
    entry_cost_detail: dict = field(default_factory=dict)
    fees_eth: float = 0.0
    fees_usd: float = 0.0
    last_fg0: int = 0
    last_fg1: int = 0
    last_tick: int = 0
    last_liquidity: int = 0
    last_fgi0: Optional[int] = None
    last_fgi1: Optional[int] = None
    exact_ticks: int = 0
    gated_ticks: int = 0
    seconds_in_range: float = 0.0
    seconds_tracked: float = 0.0
    last_ts: float = 0.0

    @property
    def entry_value_usd(self) -> float:
        return self.entry_eth * self.entry_price + self.entry_usd

    def position(self, meta: PoolMeta) -> LPPosition:
        return LPPosition(liquidity=self.liquidity, tick_lower=self.tick_lower, tick_upper=self.tick_upper, meta=meta)

    def in_range_tick(self, tick: int) -> bool:
        return self.tick_lower <= tick < self.tick_upper

    def accrue(self, st: PoolState, fgi: Optional[tuple[int, int]], meta: PoolMeta) -> dict:
        """Credit the fees earned since the previous observation.

        Uses the exact fee growth inside the range when the pool has data for
        both boundary ticks; otherwise the global fee growth gated by whether
        the price was inside the range (half credit when it crossed).
        """
        dg0 = (st.fg0 - self.last_fg0) % U256
        dg1 = (st.fg1 - self.last_fg1) % U256
        in_prev = self.in_range_tick(self.last_tick)
        in_cur = self.in_range_tick(st.tick)
        method = "gated"
        d0: float = 0.0
        d1: float = 0.0
        if fgi is not None and self.last_fgi0 is not None and self.last_fgi1 is not None:
            e0 = (fgi[0] - self.last_fgi0) % U256
            e1 = (fgi[1] - self.last_fgi1) % U256
            if e0 <= dg0 and e1 <= dg1:        # sane: a range cannot earn more than the whole pool
                method, d0, d1 = "exact", float(e0), float(e1)
        if method == "gated":
            frac = (float(in_prev) + float(in_cur)) / 2
            d0, d1 = dg0 * frac, dg1 * frac
        pool_l = (self.last_liquidity + st.liquidity) / 2
        dilution = pool_l / (pool_l + self.liquidity) if pool_l > 0 else 1.0
        raw0 = d0 * self.liquidity / Q128 * dilution
        raw1 = d1 * self.liquidity / Q128 * dilution
        eth, usd = meta.to_human(int(raw0), int(raw1))
        self.fees_eth += eth
        self.fees_usd += usd
        dt = max(0.0, st.ts - self.last_ts) if self.last_ts else 0.0
        self.seconds_tracked += dt
        self.seconds_in_range += dt * (float(in_prev) + float(in_cur)) / 2
        if method == "exact":
            self.exact_ticks += 1
        else:
            self.gated_ticks += 1
        self.last_fg0, self.last_fg1, self.last_tick = st.fg0, st.fg1, st.tick
        self.last_liquidity, self.last_ts = st.liquidity, st.ts
        self.last_fgi0, self.last_fgi1 = (fgi if fgi is not None else (None, None))
        return {"eth": eth, "usd": usd, "usd_total": eth * st.price + usd, "method": method, "in_range": in_cur,
                "dilution": dilution}

    def snapshot(self, price: float, meta: PoolMeta) -> dict:
        pos = self.position(meta)
        eth, usd = pos.holdings(price)
        value = eth * price + usd
        fees_total = self.fees_eth * price + self.fees_usd
        hodl = self.entry_eth * price + self.entry_usd
        return {
            "open": True,
            "chain": self.chain, "pool": self.pool,
            "tick_lower": self.tick_lower, "tick_upper": self.tick_upper,
            "price_low": pos.price_low, "price_high": pos.price_high,
            "in_range": pos.in_range(price),
            "eth": eth, "usdc": usd, "value_usd": value,
            "eth_at_lower": pos.eth_at_lower_bound(),
            "fees_eth": self.fees_eth, "fees_usdc": self.fees_usd, "fees_usd_total": fees_total,
            "entry_ts": self.entry_ts, "entry_price": self.entry_price,
            "entry_eth": self.entry_eth, "entry_usdc": self.entry_usd, "entry_value_usd": self.entry_value_usd,
            "entry_cost_usd": self.entry_cost_usd, "entry_cost_detail": self.entry_cost_detail,
            "deploy_usd": self.deploy_usd,
            "hodl_value_usd": hodl,
            "il_usd": value - hodl,                                   # impermanent loss vs holding
            "pnl_usd": value + fees_total - self.entry_value_usd - self.entry_cost_usd,
            "vs_hodl_usd": value + fees_total - hodl - self.entry_cost_usd,
            "age_days": (time.time() - self.entry_ts) / 86400,
            "time_in_range_pct": (self.seconds_in_range / self.seconds_tracked * 100) if self.seconds_tracked else None,
            "accrual": {"exact_ticks": self.exact_ticks, "gated_ticks": self.gated_ticks},
            "liquidity": str(self.liquidity),
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PaperLP":
        return PaperLP(**{k: d[k] for k in PaperLP.__dataclass_fields__ if k in d})


def plan_paper_lp(meta: PoolMeta, price: float, deploy_usd: float, down_pct: float, up_pct: float) -> dict:
    tl, tu = meta.range_ticks(price, down_pct, up_pct)
    plan = plan_mint(meta, price, tl, tu, 1e12, 1e15, deploy_usd)
    lo, hi = meta.price_bounds(tl, tu)
    plan.update({"tick_lower": tl, "tick_upper": tu, "price_low": lo, "price_high": hi, "price": price})
    return plan


def open_paper_lp(chain: str, pool: str, st: PoolState, meta: PoolMeta, fee_rate: float, deploy_usd: float,
                  down_pct: float, up_pct: float, simulate_costs: bool, gas_price_wei: int, gas_units: int,
                  fgi: Optional[tuple[int, int]]) -> PaperLP:
    """Create a paper position centred on the current price, funded from USD cash:
    half of it is swapped into ETH through the same pool (fee + impact), then minted."""
    plan = plan_paper_lp(meta, st.price, deploy_usd, down_pct, up_pct)
    detail: dict = {"swap_usd": 0.0, "swap_fee_usd": 0.0, "impact_usd": 0.0, "gas_usd": 0.0}
    cost = 0.0
    if simulate_costs:
        swap_value = plan["eth"] * st.price
        sc = swap_cost_usd(swap_value, st.price, st.liquidity, meta, fee_rate, sell_eth=False)
        gas = gas_cost_usd(gas_price_wei, gas_units, st.price)
        detail = {"swap_usd": swap_value, "swap_fee_usd": sc["fee_usd"], "impact_usd": sc["impact_usd"],
                  "impact_pct": sc["impact_pct"], "gas_usd": gas}
        cost = sc["total_usd"] + gas
    lp = PaperLP(chain=chain, pool=pool, liquidity=plan["liquidity"], tick_lower=plan["tick_lower"],
                 tick_upper=plan["tick_upper"], deploy_usd=deploy_usd, entry_ts=st.ts, entry_block=st.block,
                 entry_price=st.price, entry_eth=plan["eth"], entry_usd=plan["usd"], entry_cost_usd=cost,
                 entry_cost_detail=detail, last_fg0=st.fg0, last_fg1=st.fg1, last_tick=st.tick,
                 last_liquidity=st.liquidity, last_ts=st.ts)
    if fgi is not None:
        lp.last_fgi0, lp.last_fgi1 = fgi
    return lp


def close_cost_usd(lp: PaperLP, st: PoolState, meta: PoolMeta, fee_rate: float, simulate_costs: bool,
                   gas_price_wei: int, gas_units: int) -> dict:
    """Cost of unwinding: gas plus swapping the ETH leg back to USD."""
    if not simulate_costs:
        return {"swap_fee_usd": 0.0, "impact_usd": 0.0, "gas_usd": 0.0, "total_usd": 0.0}
    eth, _ = lp.position(meta).holdings(st.price)
    eth += lp.fees_eth
    sc = swap_cost_usd(eth * st.price, st.price, st.liquidity, meta, fee_rate, sell_eth=True)
    gas = gas_cost_usd(gas_price_wei, gas_units, st.price)
    return {"swap_fee_usd": sc["fee_usd"], "impact_usd": sc["impact_usd"], "gas_usd": gas,
            "total_usd": sc["total_usd"] + gas}


# ---- paper hedge -----------------------------------------------------------------------------

@dataclass
class HedgeLeg:
    instrument_name: str
    amount: float
    strike: float
    expiry: int
    option_type: str
    entry_price: float          # average premium paid per contract
    entry_fee_usd: float
    entry_ts: float
    entry_index: float
    mark: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    delta: float = 0.0
    iv: float = 0.0

    @property
    def days_to_expiry(self) -> float:
        return (self.expiry - time.time()) / 86400

    def to_position(self) -> OptionPosition:
        return OptionPosition(self.instrument_name, self.amount, self.entry_price, self.mark, self.delta,
                              self.strike, self.expiry, self.option_type)


def option_fee_usd(ticker: Ticker, amount: float, price: float) -> float:
    """Taker fee as Derive charges it: rate x index per contract, capped at a share
    of the premium, plus the flat per-order base fee."""
    inst = ticker.instrument
    per_contract = min(ticker.taker_fee_rate * (ticker.index_price or 0), OPTION_FEE_CAP * price)
    return per_contract * amount + (inst.base_fee or 0.0)


def fill_price(ticker: Ticker, side: str, amount: float, slippage_pct: float) -> tuple[float, list[str]]:
    """Price a taker actually gets: top of book for what it holds, worse for the rest."""
    warnings: list[str] = []
    slip = slippage_pct / 100
    if side == "buy":
        top, size = ticker.best_ask, ticker.ask_size
        if top <= 0:
            top = ticker.mark_price * (1 + slip)
            warnings.append(f"{ticker.instrument.name}: no ask; filled at mark + {slippage_pct:.0f}%")
            return top, warnings
        worse = top * (1 + slip)
    else:
        top, size = ticker.best_bid, ticker.bid_size
        if top <= 0:
            top = max(ticker.mark_price * (1 - slip), 0.0)
            warnings.append(f"{ticker.instrument.name}: no bid; filled at mark - {slippage_pct:.0f}%")
            return top, warnings
        worse = top * (1 - slip)
    if size <= 0 or amount <= size:
        return top, warnings
    rest = amount - size
    avg = (top * size + worse * rest) / amount
    warnings.append(f"{ticker.instrument.name}: only {size:g} at top of book; {rest:.3f} assumed at "
                    f"{'+' if side == 'buy' else '-'}{slippage_pct:.0f}%")
    return avg, warnings


@dataclass
class PaperHedge:
    legs: list[HedgeLeg] = field(default_factory=list)
    premium_paid_usd: float = 0.0
    fees_paid_usd: float = 0.0
    premium_received_usd: float = 0.0
    settled_payout_usd: float = 0.0
    trades: list[dict] = field(default_factory=list)

    # ---- views --------------------------------------------------------------------------
    def positions(self) -> list[OptionPosition]:
        return [l.to_position() for l in self.legs if l.amount > 1e-12]

    def leg(self, name: str) -> Optional[HedgeLeg]:
        return next((l for l in self.legs if l.instrument_name == name and l.amount > 1e-12), None)

    def value_usd(self) -> float:
        return sum(l.amount * l.mark for l in self.legs)

    def liquidation_value_usd(self) -> float:
        return sum(l.amount * (l.bid if l.bid > 0 else l.mark) for l in self.legs)

    @property
    def net_cost_usd(self) -> float:
        """Cash out of pocket so far: premium + fees paid, less premium received and settlements."""
        return self.premium_paid_usd + self.fees_paid_usd - self.premium_received_usd - self.settled_payout_usd

    def pnl_usd(self) -> float:
        return self.value_usd() - self.net_cost_usd

    def summary(self) -> dict:
        return {
            "legs": [asdict(l) | {"days_to_expiry": l.days_to_expiry, "value_usd": l.amount * l.mark,
                                  "cost_usd": l.amount * l.entry_price + l.entry_fee_usd,
                                  "pnl_usd": l.amount * (l.mark - l.entry_price) - l.entry_fee_usd}
                     for l in self.legs if l.amount > 1e-12],
            "held_contracts": sum(l.amount for l in self.legs if l.option_type == "P"),
            "premium_paid_usd": self.premium_paid_usd,
            "fees_paid_usd": self.fees_paid_usd,
            "premium_received_usd": self.premium_received_usd,
            "settled_payout_usd": self.settled_payout_usd,
            "net_cost_usd": self.net_cost_usd,
            "value_usd": self.value_usd(),
            "liquidation_value_usd": self.liquidation_value_usd(),
            "pnl_usd": self.pnl_usd(),
            "trades": self.trades[-50:][::-1],
        }

    # ---- actions ------------------------------------------------------------------------
    def buy(self, ticker: Ticker, amount: float, slippage_pct: float, ts: Optional[float] = None) -> dict:
        ts = ts or time.time()
        inst = ticker.instrument
        amount = float(round_amount(Decimal(str(amount)), inst.amount_step))
        if amount <= 0:
            raise ValueError("amount rounds to zero")
        price, warnings = fill_price(ticker, "buy", amount, slippage_pct)
        fee = option_fee_usd(ticker, amount, price)
        leg = self.leg(inst.name)
        if leg is None:
            leg = HedgeLeg(inst.name, 0.0, inst.strike, inst.expiry, inst.option_type, 0.0, 0.0, ts,
                           ticker.index_price)
            self.legs.append(leg)
        total = leg.amount + amount
        leg.entry_price = (leg.entry_price * leg.amount + price * amount) / total
        leg.entry_fee_usd += fee
        leg.amount = total
        leg.mark, leg.bid, leg.ask, leg.delta, leg.iv = ticker.mark_price, ticker.best_bid, ticker.best_ask, ticker.delta, ticker.iv
        self.premium_paid_usd += price * amount
        self.fees_paid_usd += fee
        tr = {"ts": ts, "side": "buy", "instrument": inst.name, "amount": amount, "price": price, "fee_usd": fee,
              "index": ticker.index_price, "mark": ticker.mark_price, "ask": ticker.best_ask, "bid": ticker.best_bid,
              "spread_cost_usd": max(0.0, price - ticker.mark_price) * amount, "warnings": warnings}
        self.trades.append(tr)
        return tr

    def sell(self, ticker: Ticker, amount: float, slippage_pct: float, ts: Optional[float] = None) -> dict:
        ts = ts or time.time()
        inst = ticker.instrument
        leg = self.leg(inst.name)
        if leg is None:
            raise ValueError(f"no paper position in {inst.name}")
        amount = min(float(round_amount(Decimal(str(amount)), inst.amount_step)), leg.amount)
        price, warnings = fill_price(ticker, "sell", amount, slippage_pct)
        fee = option_fee_usd(ticker, amount, price)
        leg.amount -= amount
        leg.mark, leg.bid, leg.ask, leg.delta, leg.iv = ticker.mark_price, ticker.best_bid, ticker.best_ask, ticker.delta, ticker.iv
        self.premium_received_usd += price * amount
        self.fees_paid_usd += fee
        tr = {"ts": ts, "side": "sell", "instrument": inst.name, "amount": amount, "price": price, "fee_usd": fee,
              "index": ticker.index_price, "mark": ticker.mark_price, "ask": ticker.best_ask, "bid": ticker.best_bid,
              "spread_cost_usd": max(0.0, ticker.mark_price - price) * amount, "warnings": warnings}
        self.trades.append(tr)
        self.legs = [l for l in self.legs if l.amount > 1e-12]
        return tr

    def mark_to_market(self, tickers: dict[str, Ticker]) -> None:
        for l in self.legs:
            tk = tickers.get(l.instrument_name)
            if tk:
                l.mark, l.bid, l.ask, l.delta, l.iv = tk.mark_price, tk.best_bid, tk.best_ask, tk.delta, tk.iv

    def settle_expired(self, index_price: float, now: Optional[float] = None) -> list[dict]:
        """Pay out intrinsic value for legs past expiry and remove them."""
        now = now or time.time()
        out = []
        keep = []
        for l in self.legs:
            if l.expiry > now or l.amount <= 1e-12:
                keep.append(l)
                continue
            intrinsic = max(l.strike - index_price, 0.0) if l.option_type == "P" else max(index_price - l.strike, 0.0)
            payout = intrinsic * l.amount
            self.settled_payout_usd += payout
            tr = {"ts": now, "side": "settle", "instrument": l.instrument_name, "amount": l.amount, "price": intrinsic,
                  "fee_usd": 0.0, "index": index_price, "payout_usd": payout,
                  "pnl_usd": payout - l.amount * l.entry_price - l.entry_fee_usd, "warnings": []}
            self.trades.append(tr)
            out.append(tr)
        self.legs = keep
        return out

    # ---- persistence ----------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"legs": [asdict(l) for l in self.legs], "premium_paid_usd": self.premium_paid_usd,
                "fees_paid_usd": self.fees_paid_usd, "premium_received_usd": self.premium_received_usd,
                "settled_payout_usd": self.settled_payout_usd, "trades": self.trades[-500:]}

    @staticmethod
    def from_dict(d: dict) -> "PaperHedge":
        h = PaperHedge()
        h.legs = [HedgeLeg(**{k: x[k] for k in HedgeLeg.__dataclass_fields__ if k in x}) for x in d.get("legs", [])]
        for k in ("premium_paid_usd", "fees_paid_usd", "premium_received_usd", "settled_payout_usd"):
            setattr(h, k, float(d.get(k, 0.0)))
        h.trades = list(d.get("trades", []))
        return h


def round_amount(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding="ROUND_DOWN") * step


# ---- projection --------------------------------------------------------------------------------

def projection(range_stats: dict, pool_stats: dict, liquidity: int, deploy_usd: float, entry_cost_usd: float,
               hedge_quote: Optional[dict], horizon_days: float, roll_days: float) -> dict:
    """Turn the last few days of pool history plus today's option quote into
    "what would I make" numbers. Everything is an extrapolation of the past.
    """
    fee_day = range_stats.get("fees_per_day_usd", 0.0)
    covered = range_stats.get("days_covered", 0.0)
    out: dict = {
        "based_on_days": covered,
        "time_in_range_pct": range_stats.get("time_in_range_pct", 0.0),
        "fees_lookback_usd": range_stats.get("fees_usd", 0.0),
        "fees_per_day_usd": fee_day,
        "fee_apr_pct": (fee_day * 365 / deploy_usd * 100) if deploy_usd > 0 else 0.0,
        "pool_share_pct": (liquidity / (pool_stats.get("avg_liquidity", 0) + liquidity) * 100)
        if pool_stats.get("avg_liquidity") else None,
        "horizon_days": horizon_days,
        "entry_cost_usd": entry_cost_usd,
    }
    hedge_day = 0.0
    if hedge_quote and hedge_quote.get("contracts", 0) > 0 and hedge_quote.get("price", 0) > 0:
        c = hedge_quote["contracts"]
        prem = c * hedge_quote["price"]
        fee = hedge_quote.get("fee_usd", 0.0)
        dte = max(float(hedge_quote.get("days_to_expiry") or 0.0), 0.5)
        held_days = max(dte - roll_days, 1.0)              # we roll before expiry, so premium covers fewer days
        hedge_day = (prem + fee) / held_days
        out["hedge"] = {
            "instrument": hedge_quote.get("instrument"), "contracts": c, "price": hedge_quote["price"],
            "premium_usd": prem, "fee_usd": fee, "days_to_expiry": dte, "held_days": held_days,
            "cost_per_day_usd": hedge_day,
            "premium_pct_of_lp": (prem + fee) / deploy_usd * 100 if deploy_usd else 0.0,
            "fees_cover_hedge_pct": (fee_day / hedge_day * 100) if hedge_day > 0 else None,
            "breakeven_days": ((prem + fee) / fee_day) if fee_day > 0 else None,
            "rolls_in_horizon": horizon_days / held_days,
        }
    else:
        out["hedge"] = None
    out["hedge_cost_per_day_usd"] = hedge_day
    out["net_carry_per_day_usd"] = fee_day - hedge_day
    out["horizon"] = {
        "fees_usd": fee_day * horizon_days,
        "hedge_cost_usd": hedge_day * horizon_days,
        "entry_cost_usd": entry_cost_usd,
        "net_usd": fee_day * horizon_days - hedge_day * horizon_days - entry_cost_usd,
        "net_pct": ((fee_day - hedge_day) * horizon_days - entry_cost_usd) / deploy_usd * 100 if deploy_usd else 0.0,
    }
    return out
