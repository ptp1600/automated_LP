import time

import pytest

from lp_hedger.chains import CHAINS, get_pool
from lp_hedger.history import DAY, Interval, PoolHistory, backfill
from lp_hedger.pool import Q128, PoolReader, PoolState, Swap
from lp_hedger.uniswap_math import PoolMeta

META = PoolMeta(eth_is_token0=True, usd_decimals=6, tick_spacing=10)
FEE = 0.0005
NOW = 1_800_000_000.0


def state(ts, price, fg0, fg1, liq=10**18, block=100):
    return PoolState(block=block, ts=ts, sqrt_price_x96=0, tick=META.tick_from_price(price), price=price,
                     liquidity=liq, fg0=fg0, fg1=fg1)


def test_interval_from_states_volume_and_fees():
    # Fee growth of 1e-6 ETH-units per unit L on token0 (ETH) and 2 USDC-units on token1
    L = 10**18
    a = state(NOW, 2500, 0, 0, liq=L)
    b = state(NOW + 3600, 2600, int(1e12 * Q128), int(2 * Q128), liq=L)
    iv = Interval.from_states(a, b, META, FEE, "live")
    assert iv.seconds == 3600 and iv.source == "live"
    assert iv.price_min == 2500 and iv.price_max == 2600 and iv.price == 2600
    # fees: 1e12 raw wei per unit L x 1e18 L = 1e30 wei = 1e12 ETH?? no: to_human divides by 1e18 -> 1e12 wei... check scale
    fees_eth, fees_usd = META.to_human(int(1e12 * L), int(2 * L))
    assert iv.fees_usd == pytest.approx(fees_eth * 2600 + fees_usd)
    assert iv.volume_usd == pytest.approx(iv.fees_usd / FEE)


def test_interval_from_swaps_scales_sampled_window():
    sp = int(META.raw_sqrt_from_price(2500) * 2**96)
    L = 10**18
    swaps = [Swap(1, 10**18, -2490 * 10**6, sp, L, META.tick_from_price(2500)),       # sell 1 ETH into the pool
             Swap(2, -(10**18), 2510 * 10**6, sp, L, META.tick_from_price(2500))]     # buy 1 ETH with 2510 USDC
    iv = Interval.from_swaps(swaps, NOW, NOW + 4 * 3600, META, FEE, sample_frac=0.25)
    # observed volume ~ 2500 + 2510 USD, scaled x4
    assert iv.volume_usd == pytest.approx((2500 + 2510) * 4, rel=1e-6)
    assert iv.fees_usd == pytest.approx(iv.volume_usd * FEE)
    assert iv.fee_per_l0 == pytest.approx(10**18 * FEE / L * 4)
    assert iv.fee_per_l1 == pytest.approx(2510 * 10**6 * FEE / L * 4)
    assert iv.liquidity == pytest.approx(L)
    empty = Interval.from_swaps([], NOW, NOW + 10, META, FEE, fallback_price=2400)
    assert empty.volume_usd == 0 and empty.price == 2400


def test_in_range_fraction():
    iv = Interval(0, 10, 2500, 2400, 2600, 1, 0, 0, 0, 0, "live")
    assert iv.in_range_fraction(2000, 3000) == 1.0
    assert iv.in_range_fraction(2500, 3000) == pytest.approx(0.5)
    assert iv.in_range_fraction(2700, 3000) == 0.0
    flat = Interval(0, 10, 2500, 2500, 2500, 1, 0, 0, 0, 0, "live")
    assert flat.in_range_fraction(2400, 2600) == 1.0 and flat.in_range_fraction(2600, 2700) == 0.0


def test_add_live_merges_into_hourly_buckets_and_prunes():
    h = PoolHistory()
    t = NOW
    for i in range(120):   # 120 one-minute live intervals
        h.add_live(Interval(t, t + 60, 2500 + i, 2500 + i, 2500 + i, 10**18, 1.0, 1.0, 100.0, 0.05, "live"))
        t += 60
    assert len(h.intervals) == 2
    assert h.intervals[0].seconds == 3600 and h.intervals[0].volume_usd == pytest.approx(6000)
    assert h.intervals[0].fee_per_l0 == pytest.approx(60) and h.intervals[0].price_max == 2559
    h.add(Interval(NOW - 30 * DAY, NOW - 29 * DAY, 1, 1, 1, 1, 0, 0, 0, 0, "archive"))
    h.prune(now=t)
    assert all(i.source == "live" for i in h.intervals)


def test_range_stats_and_daily_use_time_in_range_and_dilution():
    h = PoolHistory()
    L_pool = 10**18
    # two days: day 1 price in range, day 2 price way above the range; same fee growth both days
    for k, price in ((2, 2500), (1, 3500)):
        t0 = NOW - k * DAY
        h.add(Interval(t0, t0 + DAY, price, price, price, L_pool, 0.0, float(1000 * 10**6), 1e6, 500.0, "archive"))
    L_ours = 10**16  # 1% of the pool
    rs = h.range_stats(2400, 2600, L_ours, META, days=2, now=NOW, price_now=2500)
    assert rs["days_covered"] == pytest.approx(2)
    assert rs["time_in_range_pct"] == pytest.approx(50)
    # only day 1 pays: 1000 USDC per unit L x 1e16 L / 1e6 = 1e13 USDC?? -> to_human: raw1 = 1000e6 * 1e16 = 1e25 -> /1e6 = 1e19 USD. Use ratio instead:
    expected = 1000 * 10**6 * L_ours / 10**6 * (L_pool / (L_pool + L_ours))
    assert rs["fees_usd"] == pytest.approx(expected, rel=1e-9)
    assert rs["fees_per_day_usd"] == pytest.approx(expected / 2)
    ps = h.pool_stats(2, now=NOW)
    assert ps["volume_usd"] == pytest.approx(2e6) and ps["fees_usd"] == pytest.approx(1000)
    assert ps["avg_liquidity"] == pytest.approx(L_pool)
    days = h.daily(2, now=NOW, price_low=2400, price_high=2600, liquidity=L_ours, meta=META)
    assert len(days) == 3
    full = [d for d in days if d["coverage_pct"] > 99]
    assert len(full) >= 1
    in_range_day = next(d for d in days if d["time_in_range_pct"] == 100)
    out_day = next(d for d in days if d["time_in_range_pct"] == 0)
    assert in_range_day["position_fees_usd"] > 0 and out_day["position_fees_usd"] == 0


def test_history_roundtrip():
    h = PoolHistory([Interval(1, 2, 3, 3, 3, 4, 5, 6, 7, 8, "logs", 0.5)])
    h.backfill = {"method": "logs"}
    again = PoolHistory.from_dict(h.to_dict())
    assert again.intervals[0].to_dict() == h.intervals[0].to_dict() and again.backfill == h.backfill


# ---- backfill against a fake node ---------------------------------------------------------------

class FakeNode:
    """A pool whose fee growth rises linearly with time; serves archive calls and/or logs."""

    def __init__(self, archive=True, logs=True, now=NOW, bps=0.5):
        self.archive, self.logs, self.now, self.bps = archive, logs, now, bps
        self.latest = 1_000_000
        self.calls = []

    def ts(self, block):
        return self.now - (self.latest - block) / self.bps

    def post(self, url, json=None, timeout=None, headers=None):
        reqs = json if isinstance(json, list) else [json]
        out = [self.one(r) for r in reqs]
        from tests.test_rpc import FakeResponse
        return FakeResponse(out if isinstance(json, list) else out[0])

    def one(self, r):
        self.calls.append(r["method"])
        m, p = r["method"], r["params"]
        ok = lambda res: {"jsonrpc": "2.0", "id": r["id"], "result": res}
        err = lambda code, msg: {"jsonrpc": "2.0", "id": r["id"], "error": {"code": code, "message": msg}}
        if m == "eth_chainId":
            return ok(hex(8453))
        if m == "eth_blockNumber":
            return ok(hex(self.latest))
        if m == "eth_getBlockByNumber":
            n = self.latest if p[0] == "latest" else int(p[0], 16)
            return ok({"number": hex(n), "timestamp": hex(int(self.ts(n))), "baseFeePerGas": "0x1"})
        if m == "eth_gasPrice":
            return ok(hex(10**8))
        if m == "eth_call":
            blk = p[1]
            n = self.latest if blk == "latest" else int(blk, 16)
            if n != self.latest and not self.archive:
                return err(-32000, "missing trie node")
            data = p[0]["data"]
            from lp_hedger.abis import SELECTORS
            from eth_abi.abi import encode
            sel = {v: k for k, v in SELECTORS.items()}[data[:10]]
            sp = int(META.raw_sqrt_from_price(2500) * 2**96)
            if sel == "getPool":
                return ok("0x" + "00" * 12 + "ab" * 20)
            if sel == "slot0":
                return ok("0x" + encode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                                        [sp, META.tick_from_price(2500), 0, 0, 0, 0, True]).hex())
            if sel == "token0":
                return ok("0x" + encode(["address"], [CHAINS["base"].weth]).hex())
            if sel == "token1":
                return ok("0x" + encode(["address"], [CHAINS["base"].usdc]).hex())
            if sel == "fee":
                return ok("0x" + encode(["uint24"], [500]).hex())
            if sel == "tickSpacing":
                return ok("0x" + encode(["int24"], [10]).hex())
            if sel == "liquidity":
                return ok("0x" + encode(["uint128"], [10**18]).hex())
            if sel in ("feeGrowthGlobal0X128", "feeGrowthGlobal1X128"):
                # 1 USDC-unit (token1) per unit L per hour; token0 flat
                growth = 0 if sel.endswith("0X128") else int(self.ts(n) / 3600 * 10**6 * Q128)
                return ok("0x" + encode(["uint256"], [growth]).hex())
            if sel == "ticks":
                return ok("0x" + encode(["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"],
                                        [0, 0, 0, 0, 0, 0, 0, False]).hex())
        if m == "eth_getLogs":
            if not self.logs:
                return err(-32602, "Archive requests require a personal token")
            f, t = int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)
            if t - f > 2000:
                return err(-32614, "eth_getLogs is limited to a 2,000 range")
            from eth_abi.abi import encode
            sp = int(META.raw_sqrt_from_price(2500) * 2**96)
            logs = []
            for b in range(f, t, 50):            # one swap of 1 ETH every 50 blocks (100 s)
                data = encode(["int256", "int256", "uint160", "uint128", "int24"],
                              [10**18, -2500 * 10**6, sp, 10**18, META.tick_from_price(2500)])
                logs.append({"blockNumber": hex(b), "data": "0x" + data.hex(), "topics": []})
            return ok(logs)
        return err(-32601, "nope")


def reader_with(node):
    import requests
    chain = CHAINS["base"]
    r = PoolReader(chain, get_pool(chain, "ETH/USDC 0.05%"))
    r.rpc.http = node
    return r


def _patch_sessions(monkeypatch, node):
    import lp_hedger.rpc as rpcmod
    monkeypatch.setattr(rpcmod.requests, "Session", lambda: node)


def test_backfill_prefers_archive(monkeypatch):
    node = FakeNode(archive=True, logs=True)
    _patch_sessions(monkeypatch, node)
    r = reader_with(node)
    ivs, info = backfill(r, NOW - 2 * DAY, NOW, per_day=4, rpc_urls=["http://fake"])
    assert info["method"] == "archive" and len(ivs) == 8
    assert all(i.source == "archive" for i in ivs)
    # 1 USDC per unit L per hour x 1e18 L -> 1e12 USDC/h... volume = fees / fee_rate; just check monotone sanity
    assert all(i.fees_usd > 0 and i.volume_usd == pytest.approx(i.fees_usd / 0.0005) for i in ivs)
    assert abs(sum(i.seconds for i in ivs) - 2 * DAY) < 60


def test_backfill_falls_back_to_logs(monkeypatch):
    node = FakeNode(archive=False, logs=True)
    _patch_sessions(monkeypatch, node)
    r = reader_with(node)
    ivs, info = backfill(r, NOW - 1 * DAY, NOW, per_day=6, rpc_urls=["http://fake"])
    assert info["method"] == "logs" and len(ivs) == 6
    iv = ivs[0]
    assert iv.sample_frac < 1 and iv.volume_usd > 0 and iv.price == pytest.approx(2500, rel=1e-6)
    # one 1-ETH swap per 100 s, scaled to a 4-hour slot: 144 ETH x 2500
    assert iv.volume_usd == pytest.approx(144 * 2500, rel=0.1)


def test_backfill_reports_failure(monkeypatch):
    node = FakeNode(archive=False, logs=False)
    _patch_sessions(monkeypatch, node)
    r = reader_with(node)
    ivs, info = backfill(r, NOW - DAY, NOW, rpc_urls=["http://fake"])
    assert ivs == [] and info["method"] is None and "fake" in info["note"]
