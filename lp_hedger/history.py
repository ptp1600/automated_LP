"""Pool activity history: volume, fees per unit liquidity, price path.

The history is a list of time ``Interval``s. Each one says, for a span of
time, how much fee growth per unit of liquidity the pool produced (the exact
quantity an LP earns), the active liquidity, the estimated swap volume and
the price range traded. From that we can say what *any* range with *any*
size would have earned, and extrapolate.

Three sources feed intervals:

* **live** — consecutive polls of the pool state while the app runs
  (``Interval.from_states``), merged into hourly buckets;
* **archive** backfill — the same state read at historical blocks, on nodes
  that keep old state;
* **logs** backfill — raw ``Swap`` events in sampled windows, on nodes that
  serve old logs but not old state. Each swap carries the active liquidity,
  so fees per unit liquidity are exact inside the window; the window is then
  scaled up to the slot it represents.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Optional

from .chains import ChainPreset
from .pool import Q128, U256, PoolReader, PoolState, Swap
from .rpc import ArchiveUnsupported, JsonRpc, LogRangeTooWide, RpcError
from .uniswap_math import PoolMeta

DAY = 86400.0
Log = Callable[[str, str], None]


@dataclass
class Interval:
    t0: float
    t1: float
    price: float                # representative ETH price (close for state-based, VWAP-ish for swaps)
    price_min: float
    price_max: float
    liquidity: float            # average active liquidity (raw units)
    fee_per_l0: float           # token0 fee units (raw) earned per unit of liquidity
    fee_per_l1: float
    volume_usd: float
    fees_usd: float             # fees earned by the whole pool
    source: str                 # live | archive | logs
    sample_frac: float = 1.0    # fraction of the span actually observed (logs sampling)

    @property
    def seconds(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def in_range_fraction(self, price_low: float, price_high: float) -> float:
        """Share of this interval the price spent inside [low, high] (linear approximation)."""
        lo, hi = self.price_min, self.price_max
        if hi <= lo:
            return 1.0 if price_low <= self.price <= price_high else 0.0
        overlap = min(hi, price_high) - max(lo, price_low)
        return max(0.0, min(1.0, overlap / (hi - lo)))

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Interval":
        return Interval(**{k: d[k] for k in Interval.__dataclass_fields__ if k in d})

    # ---- constructors --------------------------------------------------------------
    @staticmethod
    def from_states(a: PoolState, b: PoolState, meta: PoolMeta, fee_rate: float, source: str) -> "Interval":
        """Interval between two observations of the pool's global accumulators."""
        d0 = (b.fg0 - a.fg0) % U256
        d1 = (b.fg1 - a.fg1) % U256
        fee_per_l0, fee_per_l1 = d0 / Q128, d1 / Q128
        liq = (a.liquidity + b.liquidity) / 2
        fees0_raw, fees1_raw = fee_per_l0 * liq, fee_per_l1 * liq
        fees_eth, fees_usdc = meta.to_human(int(fees0_raw), int(fees1_raw))
        fees_usd = fees_eth * b.price + fees_usdc
        volume = fees_usd / fee_rate if fee_rate > 0 else 0.0
        return Interval(t0=a.ts, t1=b.ts, price=b.price, price_min=min(a.price, b.price),
                        price_max=max(a.price, b.price), liquidity=liq, fee_per_l0=fee_per_l0, fee_per_l1=fee_per_l1,
                        volume_usd=volume, fees_usd=fees_usd, source=source)

    @staticmethod
    def from_swaps(swaps: Iterable[Swap], t0: float, t1: float, meta: PoolMeta, fee_rate: float,
                   sample_frac: float = 1.0, fallback_price: Optional[float] = None) -> "Interval":
        """Interval from raw swap events observed in a window covering ``sample_frac`` of [t0, t1]."""
        fee_l0 = fee_l1 = 0.0
        vol_usd = fees_usd = 0.0
        liq_w = 0.0
        pmin, pmax = math.inf, -math.inf
        last_price = fallback_price or 0.0
        n = 0
        for s in swaps:
            price = meta.price_from_sqrt_x96(s.sqrt_price_x96)
            if s.liquidity <= 0:
                continue
            fee0 = max(s.amount0, 0) * fee_rate
            fee1 = max(s.amount1, 0) * fee_rate
            fee_l0 += fee0 / s.liquidity
            fee_l1 += fee1 / s.liquidity
            in_eth, in_usd = meta.to_human(max(s.amount0, 0), max(s.amount1, 0))
            v = in_eth * price + in_usd
            vol_usd += v
            fees_usd += v * fee_rate
            liq_w += s.liquidity * v
            pmin, pmax = min(pmin, price), max(pmax, price)
            last_price = price
            n += 1
        scale = 1.0 / sample_frac if sample_frac > 0 else 1.0
        if n == 0:
            p = last_price
            return Interval(t0, t1, p, p, p, 0.0, 0.0, 0.0, 0.0, 0.0, "logs", sample_frac)
        avg_liq = liq_w / vol_usd if vol_usd > 0 else 0.0
        return Interval(t0=t0, t1=t1, price=last_price, price_min=pmin, price_max=pmax, liquidity=avg_liq,
                        fee_per_l0=fee_l0 * scale, fee_per_l1=fee_l1 * scale, volume_usd=vol_usd * scale,
                        fees_usd=fees_usd * scale, source="logs", sample_frac=sample_frac)


# ---- aggregate -------------------------------------------------------------------------

@dataclass
class PoolHistory:
    intervals: list[Interval] = field(default_factory=list)
    backfill: dict = field(default_factory=dict)      # {method, rpc, at, note}
    max_days: float = 8.0

    # ---- maintenance -------------------------------------------------------------
    def add(self, iv: Interval) -> None:
        if iv.seconds <= 0:
            return
        self.intervals.append(iv)
        self.intervals.sort(key=lambda i: i.t0)

    def extend(self, ivs: Iterable[Interval]) -> None:
        for iv in ivs:
            self.add(iv)

    def add_live(self, iv: Interval, bucket_sec: float = 3600.0) -> None:
        """Append a live interval, merging it into the previous live bucket while that
        bucket is shorter than ``bucket_sec`` and contiguous."""
        if iv.seconds <= 0:
            return
        last = self.intervals[-1] if self.intervals else None
        if last and last.source == "live" and abs(last.t1 - iv.t0) < 1 and (iv.t1 - last.t0) <= bucket_sec:
            total = last.seconds + iv.seconds
            last.liquidity = (last.liquidity * last.seconds + iv.liquidity * iv.seconds) / total if total else iv.liquidity
            last.t1 = iv.t1
            last.price = iv.price
            last.price_min = min(last.price_min, iv.price_min)
            last.price_max = max(last.price_max, iv.price_max)
            last.fee_per_l0 += iv.fee_per_l0
            last.fee_per_l1 += iv.fee_per_l1
            last.volume_usd += iv.volume_usd
            last.fees_usd += iv.fees_usd
        else:
            self.intervals.append(iv)

    def prune(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        cutoff = now - self.max_days * DAY
        self.intervals = [i for i in self.intervals if i.t1 > cutoff]

    @property
    def last_ts(self) -> Optional[float]:
        return max((i.t1 for i in self.intervals), default=None)

    @property
    def first_ts(self) -> Optional[float]:
        return min((i.t0 for i in self.intervals), default=None)

    def window(self, days: float, now: Optional[float] = None) -> list[Interval]:
        now = now or time.time()
        start = now - days * DAY
        return [i for i in self.intervals if i.t1 > start and i.t0 < now]

    def covered_days(self, days: float, now: Optional[float] = None) -> float:
        now = now or time.time()
        start = now - days * DAY
        return sum(max(0.0, min(i.t1, now) - max(i.t0, start)) for i in self.window(days, now)) / DAY

    # ---- analytics ------------------------------------------------------------------
    def pool_stats(self, days: float = 5.0, now: Optional[float] = None) -> dict:
        now = now or time.time()
        ivs = self.window(days, now)
        covered = self.covered_days(days, now)
        vol = sum(i.volume_usd for i in ivs)
        fees = sum(i.fees_usd for i in ivs)
        liq_w = sum(i.liquidity * i.seconds for i in ivs)
        secs = sum(i.seconds for i in ivs)
        last24 = self.window(1.0, now)
        c24 = self.covered_days(1.0, now)
        return {
            "days_requested": days,
            "days_covered": covered,
            "volume_usd": vol,
            "fees_usd": fees,
            "volume_per_day_usd": vol / covered if covered > 0 else 0.0,
            "fees_per_day_usd": fees / covered if covered > 0 else 0.0,
            "volume_24h_usd": sum(i.volume_usd for i in last24) / c24 if c24 > 0 else 0.0,
            "avg_liquidity": liq_w / secs if secs > 0 else 0.0,
            "price_min": min((i.price_min for i in ivs), default=0.0),
            "price_max": max((i.price_max for i in ivs), default=0.0),
            "sources": sorted({i.source for i in ivs}),
            "backfill": self.backfill,
        }

    def range_stats(self, price_low: float, price_high: float, liquidity: int, meta: PoolMeta,
                    days: float = 5.0, now: Optional[float] = None, price_now: Optional[float] = None) -> dict:
        """What a position of ``liquidity`` in [low, high] would have earned over the window.

        Fees are the pool's fee growth per unit liquidity, times our liquidity,
        times the share of time in range, diluted by our own addition to the
        pool's active liquidity.
        """
        now = now or time.time()
        ivs = self.window(days, now)
        covered = self.covered_days(days, now)
        fees_usd = 0.0
        in_range_secs = 0.0
        total_secs = 0.0
        for i in ivs:
            frac = i.in_range_fraction(price_low, price_high)
            secs = max(0.0, min(i.t1, now) - max(i.t0, now - days * DAY))
            total_secs += secs
            in_range_secs += secs * frac
            if frac <= 0 or liquidity <= 0:
                continue
            dilution = i.liquidity / (i.liquidity + liquidity) if i.liquidity > 0 else 1.0
            raw0 = i.fee_per_l0 * liquidity * frac * dilution
            raw1 = i.fee_per_l1 * liquidity * frac * dilution
            eth, usd = meta.to_human(int(raw0), int(raw1))
            fees_usd += eth * (price_now or i.price) + usd
        tir = in_range_secs / total_secs if total_secs > 0 else 0.0
        return {
            "days_covered": covered,
            "fees_usd": fees_usd,
            "fees_per_day_usd": fees_usd / covered if covered > 0 else 0.0,
            "time_in_range_pct": tir * 100,
        }

    def daily(self, days: int = 5, now: Optional[float] = None, price_low: Optional[float] = None,
              price_high: Optional[float] = None, liquidity: int = 0, meta: Optional[PoolMeta] = None,
              tz_offset_sec: int = 0) -> list[dict]:
        """Bucket the window into calendar days (UTC + offset) for the chart."""
        now = now or time.time()
        day0 = math.floor((now + tz_offset_sec) / DAY) * DAY - tz_offset_sec    # start of today
        buckets: dict[float, dict] = {}
        for k in range(days, -1, -1):
            start = day0 - k * DAY
            buckets[start] = {"day_start": start, "volume_usd": 0.0, "fees_usd": 0.0, "position_fees_usd": 0.0,
                              "in_range_secs": 0.0, "secs": 0.0, "price_close": None}
        for i in self.window(days + 1, now):
            # split the interval across the day buckets it touches
            for start, b in buckets.items():
                end = start + DAY
                ov = min(i.t1, end) - max(i.t0, start)
                if ov <= 0 or i.seconds <= 0:
                    continue
                share = ov / i.seconds
                b["volume_usd"] += i.volume_usd * share
                b["fees_usd"] += i.fees_usd * share
                b["secs"] += ov
                b["price_close"] = i.price
                if price_low is not None and price_high is not None and meta is not None:
                    frac = i.in_range_fraction(price_low, price_high)
                    b["in_range_secs"] += ov * frac
                    if liquidity > 0 and frac > 0:
                        dil = i.liquidity / (i.liquidity + liquidity) if i.liquidity > 0 else 1.0
                        eth, usd = meta.to_human(int(i.fee_per_l0 * liquidity * frac * dil * share),
                                                 int(i.fee_per_l1 * liquidity * frac * dil * share))
                        b["position_fees_usd"] += eth * i.price + usd
        out = []
        for b in buckets.values():
            b["time_in_range_pct"] = (b["in_range_secs"] / b["secs"] * 100) if b["secs"] > 0 else None
            b["coverage_pct"] = min(100.0, b["secs"] / DAY * 100)
            out.append(b)
        return out

    # ---- persistence -------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"intervals": [i.to_dict() for i in self.intervals], "backfill": self.backfill}

    @staticmethod
    def from_dict(d: dict) -> "PoolHistory":
        h = PoolHistory()
        h.intervals = [Interval.from_dict(x) for x in d.get("intervals", [])]
        h.intervals.sort(key=lambda i: i.t0)
        h.backfill = d.get("backfill") or {}
        return h


# ---- backfill ------------------------------------------------------------------------------

def estimate_blocks_per_sec(rpc: JsonRpc, latest: dict, span_blocks: int) -> float:
    older = rpc.get_block(max(1, latest["number"] - span_blocks))
    dt = latest["timestamp"] - older["timestamp"]
    return (latest["number"] - older["number"]) / dt if dt > 0 else 1.0


def backfill_archive(reader: PoolReader, rpc: JsonRpc, t_from: float, t_to: float, per_day: int,
                     latest: dict, bps: float, log: Log) -> list[Interval]:
    """Sample the pool state at evenly spaced historical blocks; needs an archive node."""
    assert reader.meta is not None
    fee_rate = reader.pool_preset.fee_rate
    n = max(1, int(round((t_to - t_from) / DAY * per_day)))
    times = [t_from + (t_to - t_from) * k / n for k in range(n + 1)]
    states: list[PoolState] = []
    for t in times:
        blk = int(latest["number"] - (latest["timestamp"] - t) * bps)
        blk = max(1, min(latest["number"], blk))
        states.append(reader.state(blk, rpc=rpc))     # raises ArchiveUnsupported on non-archive nodes
    out = []
    for a, b in zip(states, states[1:]):
        if b.ts > a.ts:
            out.append(Interval.from_states(a, b, reader.meta, fee_rate, "archive"))
    log("info", f"history: {len(out)} archive intervals from {rpc.url}")
    return out


def backfill_logs(reader: PoolReader, rpc: JsonRpc, t_from: float, t_to: float, per_day: int,
                  latest: dict, bps: float, log: Log, window_sec: Optional[float] = None) -> list[Interval]:
    """Sample Swap events in short windows, one per slot, and scale each to its slot."""
    assert reader.meta is not None
    chain = reader.chain
    fee_rate = reader.pool_preset.fee_rate
    slot = DAY / per_day
    max_window = chain.logs_max_blocks / bps * 0.9
    window = min(window_sec or 1200.0, max_window, slot)
    n = max(1, int(round((t_to - t_from) / slot)))
    slot = (t_to - t_from) / n
    out: list[Interval] = []
    failures = 0
    empty = 0
    for k in range(n):
        s0 = t_from + k * slot
        s1 = s0 + slot
        # observe the middle of the slot
        w0 = s0 + (slot - window) / 2
        b0 = max(1, int(latest["number"] - (latest["timestamp"] - w0) * bps))
        b1 = min(latest["number"], int(b0 + window * bps))
        try:
            swaps = reader.swaps(b0, b1, rpc=rpc)
        except LogRangeTooWide:
            if window <= 120:
                raise
            return backfill_logs(reader, rpc, t_from, t_to, per_day, latest, bps, log, window_sec=window / 2)
        except ArchiveUnsupported:
            raise
        except RpcError as e:
            failures += 1
            if failures > 3:
                raise
            log("warn", f"history: window {k + 1}/{n} failed on {rpc.url}: {e}")
            continue
        if not swaps:
            # Pruned nodes answer old ranges with [] instead of an error. Treat as missing.
            empty += 1
            continue
        frac = window / slot
        out.append(Interval.from_swaps(swaps, s0, s1, reader.meta, fee_rate, sample_frac=frac))
    if not out:
        raise RpcError("no log windows succeeded")
    if empty > 0.4 * n:
        raise ArchiveUnsupported(f"{empty}/{n} log windows came back empty: node has pruned history")
    log("info", f"history: {len(out)} slots sampled from swap logs ({window / 60:.0f} min windows) on {rpc.url}")
    return out


def backfill(reader: PoolReader, t_from: float, t_to: float, per_day: int = 6, log: Optional[Log] = None,
             rpc_urls: Optional[list[str]] = None) -> tuple[list[Interval], dict]:
    """Fill [t_from, t_to] from whichever public node can serve it.

    Tries each RPC with the archive method, then the logs method. Returns the
    intervals and a description of what worked (empty list if nothing did).
    """
    log = log or (lambda level, msg: None)
    reader.resolve()
    chain: ChainPreset = reader.chain
    urls = rpc_urls or list(dict.fromkeys([*reader.candidates, *chain.rpcs]))
    notes = []
    for url in urls:
        rpc = JsonRpc(url, timeout=reader.timeout)
        try:
            if rpc.chain_id() != chain.chain_id:
                notes.append(f"{url}: wrong chain")
                continue
            latest = rpc.get_block("latest")
            bps = estimate_blocks_per_sec(rpc, latest, int(6 * 3600 / chain.block_time_sec))
        except RpcError as e:
            notes.append(f"{url}: {str(e)[:80]}")
            continue
        t_to_eff = min(t_to, latest["timestamp"])
        for method, fn in (("archive", backfill_archive), ("logs", backfill_logs)):
            try:
                ivs = fn(reader, rpc, t_from, t_to_eff, per_day, latest, bps, log)
                info = {"method": method, "rpc": url, "at": time.time(), "from": t_from, "to": t_to_eff,
                        "intervals": len(ivs), "note": ""}
                return ivs, info
            except (ArchiveUnsupported, LogRangeTooWide) as e:
                notes.append(f"{url} {method}: {str(e)[:80]}")
            except RpcError as e:
                notes.append(f"{url} {method}: {str(e)[:80]}")
            except Exception as e:  # decoding surprises etc.: try the next method/node
                notes.append(f"{url} {method}: {type(e).__name__} {str(e)[:60]}")
    log("warn", "history: no public RPC could serve the backfill; collecting live samples only")
    return [], {"method": None, "rpc": None, "at": time.time(), "from": t_from, "to": t_to, "intervals": 0,
                "note": "; ".join(notes)[:600]}
