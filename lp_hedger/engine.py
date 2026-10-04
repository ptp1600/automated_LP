"""The paper-trading loop.

One background thread polls the chosen Uniswap pool, keeps the volume / fee
history up to date, accrues fees on the paper LP, runs the hedge policy
against Derive's live option quotes with simulated fills, and records an
equity curve. The UI reads ``status()`` and queues jobs with ``enqueue()``.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .chains import ChainPreset, PoolPreset, get_chain, get_pool
from .config import Config, data_dir
from .derive import DeriveClient, DeriveError, Instrument, Ticker
from .history import DAY, Interval, PoolHistory, backfill
from .paper import (
    PaperHedge, PaperLP, close_cost_usd, fill_price, open_paper_lp, option_fee_usd, plan_paper_lp, projection,
)
from .pool import PoolReader, PoolState
from .rpc import RpcError
from .strategy import HedgeAction, decide, scenario_table
from .uniswap_math import LPPosition

INSTRUMENT_CACHE_SEC = 600
BACKFILL_RETRY_SEC = 1800
EQUITY_KEEP = 5000


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: deque[dict] = deque(maxlen=400)
        d = data_dir()
        self.events_path: Path = d / "events.jsonl"
        self.state_path: Path = d / "paper_state.json"
        self.equity_path: Path = d / "equity.jsonl"
        self.lp: Optional[PaperLP] = None
        self.hedge = PaperHedge()
        self.realized: dict[str, float] = {"lp_usd": 0.0, "hedge_usd": 0.0, "costs_usd": 0.0}
        self.closed: list[dict] = []
        self.equity: list[dict] = []
        self.history = PoolHistory()
        self._history_key: Optional[str] = None
        self._jobs: deque[tuple[str, dict]] = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reader: Optional[PoolReader] = None
        self._reader_key: Optional[str] = None
        self._derive: Optional[DeriveClient] = None
        self._derive_key: Optional[str] = None
        self._instruments: list[Instrument] = []
        self._instruments_ts = 0.0
        self._last_state: Optional[PoolState] = None
        self._backfill_attempt_ts = 0.0
        self._out_of_range_since: Optional[float] = None
        self._snapshot: dict[str, Any] = {"running": False}
        self._load_state()
        self._load_recent_events()
        self._load_equity()

    # ---- persistence -----------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text())
        except ValueError:
            return
        if raw.get("lp"):
            self.lp = PaperLP.from_dict(raw["lp"])
        if raw.get("hedge"):
            self.hedge = PaperHedge.from_dict(raw["hedge"])
        self.realized.update(raw.get("realized") or {})
        self.closed = list(raw.get("closed") or [])

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"lp": self.lp.to_dict() if self.lp else None, "hedge": self.hedge.to_dict(),
                                   "realized": self.realized, "closed": self.closed[-50:]}, indent=1))
        tmp.replace(self.state_path)

    def _history_path(self, chain: ChainPreset, pool: PoolPreset) -> Path:
        return data_dir() / f"history_{chain.key}_{pool.fee}.json"

    def _load_history(self, chain: ChainPreset, pool: PoolPreset) -> None:
        key = f"{chain.key}:{pool.fee}"
        if key == self._history_key:
            return
        p = self._history_path(chain, pool)
        self.history = PoolHistory()
        if p.exists():
            try:
                self.history = PoolHistory.from_dict(json.loads(p.read_text()))
            except ValueError:
                pass
        self._history_key = key
        self._last_state = None
        self._backfill_attempt_ts = 0.0

    def _save_history(self, chain: ChainPreset, pool: PoolPreset) -> None:
        p = self._history_path(chain, pool)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.history.to_dict()))
        tmp.replace(p)

    def _load_recent_events(self) -> None:
        if not self.events_path.exists():
            return
        for ln in self.events_path.read_text().splitlines()[-300:]:
            try:
                self.events.append(json.loads(ln))
            except ValueError:
                continue

    def _load_equity(self) -> None:
        if not self.equity_path.exists():
            return
        for ln in self.equity_path.read_text().splitlines()[-EQUITY_KEEP:]:
            try:
                self.equity.append(json.loads(ln))
            except ValueError:
                continue

    def _append_equity(self, point: dict) -> None:
        self.equity.append(point)
        if len(self.equity) > EQUITY_KEEP:
            self.equity = self.equity[-EQUITY_KEEP:]
        with self.equity_path.open("a") as f:
            f.write(json.dumps(point) + "\n")

    def log(self, level: str, msg: str) -> None:
        ev = {"ts": time.time(), "level": level, "msg": msg}
        self.events.append(ev)
        with self.events_path.open("a") as f:
            f.write(json.dumps(ev) + "\n")

    # ---- market selection ------------------------------------------------------------
    def market(self) -> tuple[ChainPreset, PoolPreset, bool]:
        """The pool being tracked: the open paper position's pool wins over settings."""
        if self.lp is not None:
            chain = get_chain(self.lp.chain)
            pool = get_pool(chain, self.lp.pool)
            locked = (self.lp.chain, self.lp.pool) != (self.cfg.chain.chain, self.cfg.chain.pool)
            return chain, pool, locked
        chain = get_chain(self.cfg.chain.chain)
        return chain, get_pool(chain, self.cfg.chain.pool), False

    @property
    def reader(self) -> PoolReader:
        chain, pool, _ = self.market()
        key = f"{chain.key}:{pool.fee}:{self.cfg.chain.rpc_url}"
        if self._reader is None or self._reader_key != key:
            self._reader = PoolReader(chain, pool, self.cfg.chain.rpc_url, log=self.log)
            self._reader_key = key
            self._last_state = None
        return self._reader

    @property
    def derive(self) -> DeriveClient:
        d = self.cfg.derive
        key = f"{d.api_version}-{d.environment}"
        if self._derive is None or self._derive_key != key:
            self._derive = DeriveClient.from_settings(d.api_version, d.environment, None, "", 0, dry_run=True,
                                                      log=self.log)
            self._derive_key = key
            self._instruments, self._instruments_ts = [], 0.0
        return self._derive

    def rebuild_clients(self) -> None:
        """Called after the config changes."""
        self._reader = None
        self._derive = None
        self._wake.set()

    # ---- lifecycle ----------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lp-paper-engine", daemon=True)
        self._thread.start()
        self.log("info", "paper trading engine started")

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        self._thread = None
        self.log("info", "engine stopped")

    def enqueue(self, job: str, **params: Any) -> None:
        self._jobs.append((job, params))
        self._wake.set()

    def status(self) -> dict:
        snap = dict(self._snapshot)
        snap.update({
            "running": self.running,
            "paper": True,
            "events": list(self.events)[-120:][::-1],
            "pending_jobs": [j for j, _ in self._jobs],
            "realized": self.realized,
            "closed": self.closed[-20:][::-1],
            "equity": thin(self.equity, 500),
        })
        return snap

    # ---- main loop ------------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the loop alive, surface the error
                msg = str(e)
                if ("429" in msg or "rate" in msg.lower() or "timed out" in msg.lower()) and self._reader is not None:
                    new = self._reader.rotate_rpc()
                    if new:
                        self.log("warn", f"RPC trouble; switched to {new}")
                self.log("error", f"tick failed: {msg[:300]}")
                self._snapshot["last_error"] = msg[:300]
                self._snapshot["last_error_detail"] = traceback.format_exc()[-2000:]
            self._wake.wait(timeout=max(5, self.cfg.engine.poll_interval_sec))
            self._wake.clear()

    def tick(self) -> None:
        snap: dict[str, Any] = {"last_tick": time.time(), "last_error": None}
        chain, pool, locked = self.market()
        reader = self.reader
        self._load_history(chain, pool)
        try:
            reader.resolve()
            st = reader.state()
        except RpcError as e:
            new = reader.rotate_rpc()
            if new:
                self.log("warn", f"RPC {e!s:.80}; switched to {new}")
                reader.resolve()
                st = reader.state()
            else:
                raise
        meta = reader.meta
        assert meta is not None
        fee_rate = pool.fee_rate
        gas_wei = reader.gas_price_wei()
        snap["tracking"] = {"chain": chain.key, "chain_name": chain.name, "pool": pool.name, "fee_pct": pool.fee / 10000,
                            "pool_address": reader.address, "explorer": chain.explorer, "rpc": reader.rpc_url,
                            "locked": locked, "eth_is_token0": meta.eth_is_token0}
        snap["market"] = {"eth_price": st.price, "tick": st.tick, "liquidity": str(st.liquidity), "block": st.block,
                          "block_ts": st.ts, "gas_price_gwei": gas_wei / 1e9}

        # ---- history: backfill gaps, then add the live interval ----------------------------
        self._update_history(reader, st, meta, fee_rate, chain, pool)

        # ---- queued one-off jobs ----------------------------------------------------------------
        while self._jobs:
            job, params = self._jobs.popleft()
            try:
                self._run_job(job, params, st, reader, meta, fee_rate, chain, pool)
            except Exception as e:
                self.log("error", f"{job} failed: {e}")
                snap["last_error"] = f"{job}: {e}"

        # ---- paper LP accrual ----------------------------------------------------------------
        lp_pos: Optional[LPPosition] = None
        if self.lp is not None:
            fgi = reader.fee_growth_inside(self.lp.tick_lower, self.lp.tick_upper, st)
            acc = self.lp.accrue(st, fgi, meta)
            lp_pos = self.lp.position(meta)
            snap["lp"] = self.lp.snapshot(st.price, meta)
            snap["lp"]["last_accrual"] = acc
            self._maybe_auto_rebalance(st, reader, meta, fee_rate, chain, pool)
            if self.lp is not None:
                lp_pos = self.lp.position(meta)
                snap["lp"] = self.lp.snapshot(st.price, meta) | {"last_accrual": acc}
        if self.lp is None:
            c = self.cfg.lp
            plan = plan_paper_lp(meta, st.price, c.deploy_usd, c.range_down_pct, c.range_up_pct)
            snap["lp"] = {"open": False, "plan": {k: (str(v) if isinstance(v, int) and k == "liquidity" else v)
                                                   for k, v in plan.items()}}
            plan_pos = LPPosition(plan["liquidity"], plan["tick_lower"], plan["tick_upper"], meta)
            snap["lp"]["plan"]["eth_at_lower"] = plan_pos.eth_at_lower_bound()
            proj_liq, proj_lo, proj_hi, proj_deploy = plan["liquidity"], plan["price_low"], plan["price_high"], c.deploy_usd
            proj_entry_cost = 0.0
            if c.simulate_entry_costs:
                from .paper import gas_cost_usd, swap_cost_usd
                sc = swap_cost_usd(plan["eth"] * st.price, st.price, st.liquidity, meta, fee_rate, sell_eth=False)
                proj_entry_cost = sc["total_usd"] + gas_cost_usd(gas_wei, chain.gas_open_units, st.price)
            snap["lp"]["plan"]["entry_cost_usd"] = proj_entry_cost
            hedge_lp = plan_pos
            lp_value = plan["value_usd"]
        else:
            proj_liq, proj_lo, proj_hi = self.lp.liquidity, snap["lp"]["price_low"], snap["lp"]["price_high"]
            proj_deploy, proj_entry_cost = self.lp.deploy_usd, self.lp.entry_cost_usd
            hedge_lp = lp_pos
            lp_value = snap["lp"]["value_usd"]

        # ---- hedge: mark, settle, decide, paper-fill -------------------------------------------
        snap["hedge"] = self._hedge_step(lp_pos, hedge_lp, st.price, lp_value, st)
        quote = snap["hedge"].get("quote")

        # ---- pool stats + projection -------------------------------------------------------------
        days = self.cfg.engine.history_days
        pstats = self.history.pool_stats(days, now=st.ts)
        rstats = self.history.range_stats(proj_lo, proj_hi, proj_liq, meta, days, now=st.ts, price_now=st.price)
        snap["pool"] = pstats | {"range": rstats, "fee_rate": fee_rate}
        snap["daily"] = self.history.daily(days, now=st.ts, price_low=proj_lo, price_high=proj_hi, liquidity=proj_liq,
                                           meta=meta)
        snap["projection"] = projection(rstats, pstats, proj_liq, proj_deploy, proj_entry_cost, quote,
                                        self.cfg.engine.projection_days, self.cfg.hedge.roll_days_before_expiry)
        snap["scenarios"] = scenario_table(lp_pos if lp_pos is not None else hedge_lp, st.price,
                                           self.hedge.positions())
        snap["scenario_is_plan"] = lp_pos is None

        # ---- equity curve ----------------------------------------------------------------------------
        snap["totals"] = self._totals(snap, st)
        if self.lp is not None or self.hedge.legs:
            self._append_equity({"ts": st.ts, "price": st.price, **snap["totals"]})
        self._save_state()
        self._snapshot = snap

    # ---- history ----------------------------------------------------------------------------------
    def _update_history(self, reader: PoolReader, st: PoolState, meta, fee_rate: float, chain: ChainPreset,
                        pool: PoolPreset) -> None:
        days = self.cfg.engine.history_days
        now = st.ts
        last = self.history.last_ts
        gap_from = max(now - days * DAY, last or 0.0)
        need = (now - gap_from) > 2 * 3600
        if need and now - self._backfill_attempt_ts > BACKFILL_RETRY_SEC:
            self._backfill_attempt_ts = now
            self.log("info", f"history: backfilling {(now - gap_from) / 3600:.1f} h of {chain.name} {pool.name}…")
            ivs, info = backfill(reader, gap_from, now, per_day=6, log=self.log)
            self.history.extend(ivs)
            self.history.backfill = info
            if ivs:
                self.log("info", f"history: {len(ivs)} intervals via {info['method']} from {info['rpc']}")
        if self._last_state is not None and st.ts > self._last_state.ts:
            self.history.add_live(Interval.from_states(self._last_state, st, meta, fee_rate, "live"))
        self._last_state = st
        self.history.prune(now)
        self._save_history(chain, pool)

    # ---- jobs -------------------------------------------------------------------------------------
    def _run_job(self, job: str, params: dict, st: PoolState, reader: PoolReader, meta, fee_rate: float,
                 chain: ChainPreset, pool: PoolPreset) -> None:
        if job == "open_lp":
            self._open_lp(st, reader, meta, fee_rate, chain, pool)
        elif job == "close_lp":
            self._close_lp(st, meta, fee_rate, chain, "closed by user")
        elif job == "rebalance":
            self._rebalance(st, reader, meta, fee_rate, chain, pool, "manual re-center")
        elif job == "hedge_now":
            pass  # the hedge step runs on every tick anyway
        elif job == "close_hedges":
            self._close_all_hedges(st.price)
        elif job == "reset":
            self._reset()
        elif job == "refresh_history":
            self.history = PoolHistory()
            self._history_key = None
            self._load_history(chain, pool)
            self.history = PoolHistory()
            self._backfill_attempt_ts = 0.0
            self.log("info", "history cleared; backfilling on next tick")
        else:
            self.log("warn", f"unknown job {job}")

    def _open_lp(self, st: PoolState, reader: PoolReader, meta, fee_rate: float, chain: ChainPreset,
                 pool: PoolPreset) -> None:
        if self.lp is not None:
            raise RuntimeError("a paper LP position is already open; close it first")
        c = self.cfg.lp
        fgi = None
        tl, tu = meta.range_ticks(st.price, c.range_down_pct, c.range_up_pct)
        try:
            fgi = reader.fee_growth_inside(tl, tu, st)
        except Exception:
            fgi = None
        self.lp = open_paper_lp(chain.key, pool.name, st, meta, fee_rate, c.deploy_usd, c.range_down_pct,
                                c.range_up_pct, c.simulate_entry_costs, reader.gas_price_wei(), chain.gas_open_units, fgi)
        d = self.lp.entry_cost_detail
        self.log("info", f"PAPER: opened ${c.deploy_usd:,.0f} LP on {chain.name} {pool.name}: "
                         f"{self.lp.entry_eth:.4f} ETH + {self.lp.entry_usd:,.2f} USDC in "
                         f"[{meta.price_bounds(tl, tu)[0]:,.0f}, {meta.price_bounds(tl, tu)[1]:,.0f}] at {st.price:,.2f}; "
                         f"entry cost ${self.lp.entry_cost_usd:.2f} (swap fee {d.get('swap_fee_usd', 0):.2f}, "
                         f"impact {d.get('impact_usd', 0):.2f}, gas {d.get('gas_usd', 0):.2f}); "
                         f"fee accrual {'exact' if fgi else 'in-range gated'}")
        self._out_of_range_since = None

    def _close_lp(self, st: PoolState, meta, fee_rate: float, chain: ChainPreset, reason: str) -> dict:
        if self.lp is None:
            raise RuntimeError("no paper LP position to close")
        lp = self.lp
        snap = lp.snapshot(st.price, meta)
        cost = close_cost_usd(lp, st, meta, fee_rate, self.cfg.lp.simulate_entry_costs,
                              self.reader.gas_price_wei(), chain.gas_close_units)
        pnl = snap["pnl_usd"] - cost["total_usd"]
        self.realized["lp_usd"] += pnl
        self.realized["costs_usd"] += lp.entry_cost_usd + cost["total_usd"]
        self.closed.append({"ts": st.ts, "kind": "lp", "chain": lp.chain, "pool": lp.pool, "reason": reason,
                            "deploy_usd": lp.deploy_usd, "entry_price": lp.entry_price, "exit_price": st.price,
                            "price_low": snap["price_low"], "price_high": snap["price_high"],
                            "fees_usd": snap["fees_usd_total"], "il_usd": snap["il_usd"],
                            "entry_cost_usd": lp.entry_cost_usd, "exit_cost_usd": cost["total_usd"],
                            "pnl_usd": pnl, "vs_hodl_usd": snap["vs_hodl_usd"] - cost["total_usd"],
                            "days": (st.ts - lp.entry_ts) / 86400, "time_in_range_pct": snap["time_in_range_pct"]})
        self.log("info", f"PAPER: closed LP ({reason}) at {st.price:,.2f}: fees ${snap['fees_usd_total']:.2f}, "
                         f"IL ${snap['il_usd']:+.2f}, exit cost ${cost['total_usd']:.2f} → P&L ${pnl:+.2f} "
                         f"({snap['vs_hodl_usd'] - cost['total_usd']:+.2f} vs holding)")
        self.lp = None
        self._out_of_range_since = None
        return snap

    def _rebalance(self, st: PoolState, reader: PoolReader, meta, fee_rate: float, chain: ChainPreset,
                   pool: PoolPreset, reason: str) -> None:
        if self.lp is None:
            raise RuntimeError("no paper LP position to re-center")
        self._close_lp(st, meta, fee_rate, chain, reason)
        self._open_lp(st, reader, meta, fee_rate, chain, pool)

    def _maybe_auto_rebalance(self, st: PoolState, reader: PoolReader, meta, fee_rate: float, chain: ChainPreset,
                              pool: PoolPreset) -> None:
        if self.lp is None or not self.cfg.lp.auto_rebalance:
            self._out_of_range_since = None
            return
        if self.lp.position(meta).in_range(st.price):
            self._out_of_range_since = None
            return
        self._out_of_range_since = self._out_of_range_since or st.ts
        waited_min = (st.ts - self._out_of_range_since) / 60
        if waited_min >= self.cfg.lp.rebalance_after_min:
            self.log("info", f"price {st.price:,.0f} out of range for {waited_min:.0f} min: re-centering (paper)")
            self._rebalance(st, reader, meta, fee_rate, chain, pool, f"auto re-center after {waited_min:.0f} min out of range")

    def _close_all_hedges(self, index_price: float) -> None:
        for leg in list(self.hedge.legs):
            try:
                tk = self.derive.get_ticker(leg.instrument_name)
                tr = self.hedge.sell(tk, leg.amount, self.cfg.hedge.slippage_pct)
                self.log("info", f"PAPER: sold {tr['amount']:.3f} {leg.instrument_name} @ {tr['price']:.2f} "
                                 f"(fee ${tr['fee_usd']:.2f})")
            except (DeriveError, ValueError) as e:
                self.log("error", f"closing {leg.instrument_name} failed: {e}")

    def _reset(self) -> None:
        self.lp = None
        self.hedge = PaperHedge()
        self.realized = {"lp_usd": 0.0, "hedge_usd": 0.0, "costs_usd": 0.0}
        self.closed = []
        self.equity = []
        self.equity_path.write_text("")
        self._out_of_range_since = None
        self._save_state()
        self.log("info", "paper account reset (history kept)")

    # ---- hedge ------------------------------------------------------------------------------------
    def _instruments_cached(self) -> list[Instrument]:
        if time.time() - self._instruments_ts > INSTRUMENT_CACHE_SEC or not self._instruments:
            self._instruments = self.derive.get_instruments(self.cfg.derive.currency)
            self._instruments_ts = time.time()
        return self._instruments

    def _hedge_step(self, lp_pos: Optional[LPPosition], plan_pos: Optional[LPPosition], price: float,
                    lp_value: float, st: PoolState) -> dict:
        s = self.cfg.hedge
        out: dict[str, Any] = {"enabled": s.enabled, "mode": s.mode, "connected": False, "api": None}
        try:
            out["api"] = self.derive.p.key
            instruments = self._instruments_cached()
            by_name = {i.name: i for i in instruments}
            tickers: dict[str, Ticker] = {}
            for leg in self.hedge.legs:
                inst = by_name.get(leg.instrument_name)
                tickers[leg.instrument_name] = self.derive.get_ticker(inst or leg.instrument_name)
            self.hedge.mark_to_market(tickers)
            index = self.derive.get_index_price(self.cfg.derive.currency) or price
            out["connected"] = True
            out["index_price"] = index
        except Exception as e:
            out["note"] = f"Derive unavailable: {str(e)[:160]}"
            out.update(self.hedge.summary())
            return out

        for tr in self.hedge.settle_expired(index, now=st.ts):
            self.realized["hedge_usd"] += tr["pnl_usd"]
            self.log("info", f"PAPER: {tr['instrument']} expired: payout ${tr['payout_usd']:.2f} "
                             f"(intrinsic {tr['price']:.2f} × {tr['amount']:.3f}) → leg P&L ${tr['pnl_usd']:+.2f}")

        positions = self.hedge.positions()
        if not s.enabled:
            out["note"] = "hedging disabled"
            out.update(self.hedge.summary())
            return out

        # Decide against the real position if open, else against the planned one (for the quote only)
        decide_pos = lp_pos
        target, action = decide(decide_pos, price, lp_value if lp_pos else 0.0, positions, instruments, tickers, s)
        if action.kind == "need_ticker" and action.instrument_name:
            tickers[action.instrument_name] = self.derive.get_ticker(by_name.get(action.instrument_name, action.instrument_name))
            target, action = decide(decide_pos, price, lp_value if lp_pos else 0.0, positions, instruments, tickers, s)

        # Quote for the projection: what the policy would buy for the (planned or open) position right now
        quote = None
        qpos = plan_pos if lp_pos is None else lp_pos
        if qpos is not None:
            qtarget, qaction = decide(qpos, price, lp_value, [], instruments, tickers, s)
            if qaction.kind == "need_ticker" and qaction.instrument_name:
                tickers[qaction.instrument_name] = self.derive.get_ticker(by_name.get(qaction.instrument_name, qaction.instrument_name))
                qtarget, qaction = decide(qpos, price, lp_value, [], instruments, tickers, s)
            qtk = tickers.get(qaction.instrument_name or "")
            if qtarget and qtk and qtarget.contracts > 0:
                amt = qaction.amount if qaction.kind == "buy" and qaction.amount > 0 else qtarget.contracts
                px, _ = fill_price(qtk, "buy", amt, s.slippage_pct)
                quote = {"instrument": qtk.instrument.name, "contracts": amt, "price": px, "ask": qtk.best_ask,
                         "mark": qtk.mark_price, "fee_usd": option_fee_usd(qtk, amt, px),
                         "days_to_expiry": qtk.instrument.days_to_expiry, "strike": qtk.instrument.strike,
                         "delta": qtk.delta, "iv": qtk.iv, "target_contracts": qtarget.contracts,
                         "strike_target": qtarget.strike_target, "eth_at_lower": qtarget.eth_at_lower,
                         "reason": qtarget.reason, "warnings": qaction.warnings}
        out["quote"] = quote

        if target:
            out.update({"target_contracts": target.contracts, "strike_target": target.strike_target,
                        "eth_at_lower": target.eth_at_lower, "lp_delta_eth": target.lp_delta_eth, "reason": target.reason})
        out["instrument"] = action.instrument_name
        tk = tickers.get(action.instrument_name or "")
        if tk:
            out["ticker"] = {"bid": tk.best_bid, "ask": tk.best_ask, "bid_size": tk.bid_size, "ask_size": tk.ask_size,
                             "mark": tk.mark_price, "delta": tk.delta, "iv": tk.iv, "strike": tk.instrument.strike,
                             "expiry": tk.instrument.expiry, "days_to_expiry": tk.instrument.days_to_expiry}
        out["action"] = {"kind": action.kind, "amount": action.amount, "note": action.note, "warnings": action.warnings,
                         "close": [p.instrument_name for p in action.close_positions]}
        for w in action.warnings:
            self.log("warn", w)
        if action.kind in ("buy", "sell", "roll"):
            self._execute_paper_hedge(action, tk, by_name)
        out.update(self.hedge.summary())
        return out

    def _execute_paper_hedge(self, action: HedgeAction, tk: Optional[Ticker], by_name: dict[str, Instrument]) -> None:
        slip = self.cfg.hedge.slippage_pct
        for pos in action.close_positions:
            try:
                ptk = self.derive.get_ticker(by_name.get(pos.instrument_name, pos.instrument_name))
                tr = self.hedge.sell(ptk, pos.amount, slip)
                self._log_trade(tr, "roll: sold")
            except (DeriveError, ValueError) as e:
                self.log("error", f"closing {pos.instrument_name} failed: {e}")
        if action.amount > 0 and tk is not None:
            try:
                if action.kind in ("buy", "roll"):
                    tr = self.hedge.buy(tk, action.amount, slip)
                    self._log_trade(tr, "bought")
                elif action.kind == "sell":
                    tr = self.hedge.sell(tk, action.amount, slip)
                    self._log_trade(tr, "sold")
            except ValueError as e:
                self.log("error", f"paper hedge order failed: {e}")

    def _log_trade(self, tr: dict, verb: str) -> None:
        self.log("info", f"PAPER: {verb} {tr['amount']:.3f} {tr['instrument']} @ {tr['price']:.2f} "
                         f"(mark {tr['mark']:.2f}, spread cost ${tr['spread_cost_usd']:.2f}, fee ${tr['fee_usd']:.2f})")
        for w in tr.get("warnings", []):
            self.log("warn", w)

    # ---- totals -------------------------------------------------------------------------------------
    def _totals(self, snap: dict, st: PoolState) -> dict:
        lp = snap.get("lp") or {}
        open_lp_pnl = lp.get("pnl_usd", 0.0) if lp.get("open") else 0.0
        open_lp_value = lp.get("value_usd", 0.0) if lp.get("open") else 0.0
        open_fees = lp.get("fees_usd_total", 0.0) if lp.get("open") else 0.0
        hodl_pnl = (lp.get("hodl_value_usd", 0.0) - lp.get("entry_value_usd", 0.0)) if lp.get("open") else 0.0
        hedge_pnl = self.hedge.pnl_usd()
        total = open_lp_pnl + hedge_pnl + self.realized["lp_usd"] + self.realized["hedge_usd"]
        return {
            "lp_value_usd": open_lp_value,
            "lp_fees_usd": open_fees,
            "lp_pnl_usd": open_lp_pnl,
            "lp_il_usd": lp.get("il_usd", 0.0) if lp.get("open") else 0.0,
            "hedge_value_usd": self.hedge.value_usd(),
            "hedge_net_cost_usd": self.hedge.net_cost_usd,
            "hedge_pnl_usd": hedge_pnl,
            "realized_lp_usd": self.realized["lp_usd"],
            "realized_hedge_usd": self.realized["hedge_usd"],
            "total_pnl_usd": total,
            "hodl_pnl_usd": hodl_pnl,
            "in_range": lp.get("in_range") if lp.get("open") else None,
        }


def thin(points: list[dict], n: int) -> list[dict]:
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]
