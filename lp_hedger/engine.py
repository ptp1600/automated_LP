"""The automation loop.

One background thread owns every network call and every signature so nonces
never collide. The UI talks to it through ``status()`` (read-only snapshot)
and ``enqueue()`` (jobs executed on the next tick).
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from .chains import get_chain
from .config import Config, data_dir
from .derive import DeriveClient, DeriveError, OptionPosition, Ticker
from .strategy import HedgeAction, decide, scenario_table
from .uniswap import UniswapClient
from .uniswap_math import LPPosition
from .wallet import HotWallet


class Engine:
    def __init__(self, cfg: Config, wallet: HotWallet):
        self.cfg = cfg
        self.wallet = wallet
        self.events: deque[dict] = deque(maxlen=300)
        self.events_path: Path = data_dir() / "events.jsonl"
        self.state_path: Path = data_dir() / "state.json"
        self.state: dict[str, Any] = self._load_state()
        self._jobs: deque[tuple[str, dict]] = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._uni: Optional[UniswapClient] = None
        self._derive: Optional[DeriveClient] = None
        self._snapshot: dict[str, Any] = {"running": False}
        self._out_of_range_since: Optional[float] = None
        self._load_recent_events()

    # ---- persistence -----------------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except ValueError:
                pass
        return {"token_id": None, "premium_paid_usd": 0.0, "premium_received_usd": 0.0, "fees_collected_usd": 0.0}

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def _load_recent_events(self) -> None:
        if not self.events_path.exists():
            return
        lines = self.events_path.read_text().splitlines()[-200:]
        for ln in lines:
            try:
                self.events.append(json.loads(ln))
            except ValueError:
                continue

    def log(self, level: str, msg: str) -> None:
        ev = {"ts": time.time(), "level": level, "msg": msg}
        self.events.append(ev)
        with self.events_path.open("a") as f:
            f.write(json.dumps(ev) + "\n")

    # ---- clients ------------------------------------------------------------------
    def rebuild_clients(self) -> None:
        """Called after the config changes or the wallet is unlocked."""
        with self._lock:
            self._uni = None
            self._derive = None
            self._wake.set()

    @property
    def uni(self) -> UniswapClient:
        if self._uni is None:
            chain = get_chain(self.cfg.chain.chain)
            pool = chain.pools.get(self.cfg.chain.pool) or next(iter(chain.pools.values()))
            self._uni = UniswapClient(chain, pool, self.cfg.chain.rpc_url, self.wallet,
                                      dry_run=self.cfg.engine.dry_run, slippage_pct=self.cfg.lp.slippage_pct,
                                      log=self.log)
        return self._uni

    @property
    def derive(self) -> DeriveClient:
        if self._derive is None:
            d = self.cfg.derive
            self._derive = DeriveClient(d.environment, self.wallet, d.derive_wallet, d.subaccount_id,
                                        dry_run=self.cfg.engine.dry_run, log=self.log)
        return self._derive

    def derive_configured(self) -> bool:
        return bool(self.cfg.derive.derive_wallet) and self.cfg.derive.subaccount_id > 0

    # ---- lifecycle ----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        if self.running:
            return
        if not self.wallet.unlocked:
            raise RuntimeError("Unlock the wallet before starting the engine")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lp-hedger-engine", daemon=True)
        self._thread.start()
        self.log("info", f"engine started ({'DRY RUN' if self.cfg.engine.dry_run else 'LIVE'})")

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
            "dry_run": self.cfg.engine.dry_run,
            "wallet_address": self.wallet.address,
            "wallet_unlocked": self.wallet.unlocked,
            "wallet_exists": self.wallet.exists,
            "derive_configured": self.derive_configured(),
            "state": self.state,
            "events": list(self.events)[-80:][::-1],
            "pending_jobs": [j for j, _ in self._jobs],
        })
        return snap

    # ---- main loop ----------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the loop alive, surface the error
                self.log("error", f"tick failed: {e}")
                self._snapshot["last_error"] = f"{e}"
                self._snapshot["last_error_detail"] = traceback.format_exc()[-2000:]
            self._wake.wait(timeout=max(5, self.cfg.engine.poll_interval_sec))
            self._wake.clear()

    def tick(self) -> None:
        """One pass: refresh data, run pending jobs, then apply the hedge policy."""
        snap: dict[str, Any] = {"last_tick": time.time(), "last_error": None}
        uni = self.uni
        chain = uni.chain
        snap["chain"] = {"name": chain.name, "chain_id": chain.chain_id, "explorer": chain.explorer,
                         "pool": uni.pool_preset.name, "rpc_ok": uni.connected()}
        if not snap["chain"]["rpc_ok"]:
            raise RuntimeError(f"RPC not reachable or wrong chain id for {chain.name}")
        snap["chain"]["pool_address"] = uni.pool_address
        price = uni.price()
        snap["market"] = {"eth_price": price}
        bal = uni.balances()
        snap["wallet"] = {"address": self.wallet.address, **bal,
                          "value_usd": (bal["eth"] + bal["weth"]) * price + bal["usdc"]}

        # Run queued one-off jobs first so their effects show in this tick's view
        while self._jobs:
            job, params = self._jobs.popleft()
            try:
                self._run_job(job, params, price)
            except Exception as e:
                self.log("error", f"{job} failed: {e}")
                snap["last_error"] = f"{job}: {e}"

        lp, lp_info = self._lp_snapshot(price)
        snap["lp"] = lp_info
        lp_value = lp_info.get("value_usd", 0.0) if lp_info else 0.0

        # Auto fee collection
        if lp and self.cfg.lp.auto_collect_fees_usd > 0 and lp_info.get("fees_usd_total", 0) >= self.cfg.lp.auto_collect_fees_usd:
            self._collect_fees(price)

        # Auto rebalance when out of range for long enough
        if lp and self.cfg.lp.auto_rebalance:
            if not lp.in_range(price):
                self._out_of_range_since = self._out_of_range_since or time.time()
                waited_min = (time.time() - self._out_of_range_since) / 60
                snap["lp"]["out_of_range_min"] = waited_min
                if waited_min >= self.cfg.lp.rebalance_after_min:
                    self.log("info", f"price {price:.0f} out of range for {waited_min:.0f} min: rebalancing")
                    self._rebalance(price)
                    self._out_of_range_since = None
                    lp, lp_info = self._lp_snapshot(price)
                    snap["lp"] = lp_info
            else:
                self._out_of_range_since = None

        snap["hedge"] = self._hedge_step(lp, price, lp_value)
        positions = snap["hedge"].get("_positions", [])
        snap["hedge"].pop("_positions", None)
        snap["scenarios"] = scenario_table(lp, price, positions)
        self._snapshot = snap

    # ---- LP helpers --------------------------------------------------------------------
    def _lp_snapshot(self, price: float) -> tuple[Optional[LPPosition], dict]:
        uni = self.uni
        token_id = self.state.get("token_id")
        if token_id is None:
            ids = uni.owned_position_ids()
            if ids:
                token_id = ids[-1]
                self.state["token_id"] = token_id
                self._save_state()
                self.log("info", f"adopted existing LP position #{token_id}")
        if token_id is None:
            return None, {"token_id": None}
        pos = uni.position(token_id)
        if pos is None:
            self.log("info", f"position #{token_id} has no liquidity; forgetting it")
            self.state["token_id"] = None
            self._save_state()
            return None, {"token_id": None}
        eth, usd = pos.holdings(price)
        fee_eth, fee_usd = uni.pending_fees(token_id)
        return pos, {
            "token_id": token_id,
            "tick_lower": pos.tick_lower, "tick_upper": pos.tick_upper,
            "price_low": pos.price_low, "price_high": pos.price_high,
            "in_range": pos.in_range(price),
            "eth": eth, "usdc": usd, "value_usd": eth * price + usd,
            "eth_at_lower": pos.eth_at_lower_bound(),
            "usd_at_upper": pos.value_usd(pos.price_high * 1.000001),
            "fees_eth": fee_eth, "fees_usdc": fee_usd, "fees_usd_total": fee_eth * price + fee_usd,
            "liquidity": str(pos.liquidity),
        }

    def _run_job(self, job: str, params: dict, price: float) -> None:
        if job == "open_lp":
            self._open_lp()
        elif job == "close_lp":
            self._close_lp()
        elif job == "collect_fees":
            self._collect_fees(price)
        elif job == "rebalance":
            self._rebalance(price)
        elif job == "hedge_now":
            pass  # the hedge step runs on every tick anyway
        elif job == "close_hedges":
            self._close_all_hedges()
        else:
            self.log("warn", f"unknown job {job}")

    def _open_lp(self) -> None:
        if self.state.get("token_id"):
            raise RuntimeError(f"LP position #{self.state['token_id']} already open; close it first")
        c = self.cfg
        res = self.uni.open_position(c.lp.deploy_usd, c.lp.range_down_pct, c.lp.range_up_pct, c.chain.gas_reserve_eth)
        plan = res["plan"]
        self.log("info", f"LP plan: {plan['eth']:.5f} ETH + {plan['usd']:.2f} USDC ≈ ${plan['value_usd']:,.0f} "
                         f"in [{plan['price_low']:.0f}, {plan['price_high']:.0f}] at {plan['price']:.0f}")
        if res["token_id"]:
            self.state["token_id"] = res["token_id"]
            self._save_state()
            self.log("info", f"minted LP position #{res['token_id']}")
        elif not res["tx"].dry_run:
            self.log("warn", "mint sent but token id not found in receipt; will adopt from wallet on next tick")

    def _close_lp(self) -> None:
        tid = self.state.get("token_id")
        if not tid:
            raise RuntimeError("no LP position to close")
        self.uni.close_position(tid)
        if not self.cfg.engine.dry_run:
            self.state["token_id"] = None
            self._save_state()
        self.log("info", f"closed LP position #{tid}")

    def _collect_fees(self, price: float) -> None:
        tid = self.state.get("token_id")
        if not tid:
            return
        fe, fu = self.uni.pending_fees(tid)
        self.uni.collect_fees(tid)
        if not self.cfg.engine.dry_run:
            self.state["fees_collected_usd"] = self.state.get("fees_collected_usd", 0.0) + fe * price + fu
            self._save_state()
        self.log("info", f"collected fees ≈ {fe:.5f} ETH + {fu:.2f} USDC")

    def _rebalance(self, price: float) -> None:
        """Withdraw, swap back toward a 50/50 value split, re-mint around the current price."""
        c = self.cfg
        tid = self.state.get("token_id")
        if tid:
            self.uni.close_position(tid, unwrap=False)
            if c.engine.dry_run:
                self.log("info", "[dry-run] rebalance stops here (nothing was withdrawn)")
                return
            self.state["token_id"] = None
            self._save_state()
        price = self.uni.price()
        bal = self.uni.balances()
        total = (bal["eth"] - c.chain.gas_reserve_eth + bal["weth"]) * price + bal["usdc"]
        deploy = min(c.lp.deploy_usd, total * 0.98)
        # amounts the new range needs (ratio depends on asymmetry of the range)
        meta = self.uni.meta
        tl, tu = meta.range_ticks(price, c.lp.range_down_pct, c.lp.range_up_pct)
        from .uniswap_math import plan_mint
        probe = plan_mint(meta, price, tl, tu, 1e9, 1e12, deploy)
        self.uni.rebalance_to_ratio(probe["need_eth"], probe["need_usd"], c.chain.gas_reserve_eth)
        self._open_lp()

    # ---- hedge helpers ---------------------------------------------------------------------
    def _hedge_step(self, lp: Optional[LPPosition], price: float, lp_value: float) -> dict:
        s = self.cfg.hedge
        out: dict[str, Any] = {"enabled": s.enabled, "mode": s.mode, "connected": False, "_positions": []}
        if not self.derive_configured():
            out["note"] = "Derive not configured"
            return out
        try:
            sub = self.derive.get_subaccount()
            out["connected"] = True
            out["collateral_usd"] = self.derive.collateral_usd(sub)
            out["subaccount_id"] = self.derive.subaccount_id
            positions = self.derive.get_positions(self.cfg.derive.currency)
        except DeriveError as e:
            out["note"] = f"Derive error: {e}"
            self.log("error", f"Derive: {e}")
            return out
        out["_positions"] = positions
        out["positions"] = [asdict(p) | {"days_to_expiry": p.days_to_expiry} for p in positions]
        if not s.enabled:
            out["note"] = "hedging disabled"
            return out
        instruments = self.derive.get_instruments(self.cfg.derive.currency)
        tickers: dict[str, Ticker] = {}
        target, action = decide(lp, price, lp_value, positions, instruments, tickers, s)
        if action.kind == "need_ticker" and action.instrument_name:
            tickers[action.instrument_name] = self.derive.get_ticker(action.instrument_name)
            target, action = decide(lp, price, lp_value, positions, instruments, tickers, s)
        if target:
            out.update({"target_contracts": target.contracts, "strike_target": target.strike_target,
                        "eth_at_lower": target.eth_at_lower, "lp_delta_eth": target.lp_delta_eth,
                        "reason": target.reason})
        held = sum(p.amount for p in positions if p.option_type == "P" and p.amount > 0)
        out["held_contracts"] = held
        out["instrument"] = action.instrument_name
        tk = tickers.get(action.instrument_name or "")
        if tk:
            out["ticker"] = {"bid": tk.best_bid, "ask": tk.best_ask, "mark": tk.mark_price, "delta": tk.delta,
                             "iv": tk.iv, "strike": tk.instrument.strike, "expiry": tk.instrument.expiry,
                             "days_to_expiry": tk.instrument.days_to_expiry}
        out["action"] = {"kind": action.kind, "amount": action.amount, "note": action.note,
                         "warnings": action.warnings,
                         "close": [p.instrument_name for p in action.close_positions]}
        for w in action.warnings:
            self.log("warn", w)
        if action.kind in ("buy", "sell", "roll"):
            self._execute_hedge(action, tk)
        return out

    def _execute_hedge(self, action: HedgeAction, tk: Optional[Ticker]) -> None:
        d = self.derive
        for pos in action.close_positions:
            try:
                ptk = d.get_ticker(pos.instrument_name)
                res = d.place_ioc(ptk, "sell", pos.amount, self.cfg.hedge.slippage_pct)
                self._book(res, "sell")
            except DeriveError as e:
                self.log("error", f"closing {pos.instrument_name} failed: {e}")
        if action.amount > 0 and tk is not None and action.kind in ("buy", "roll"):
            res = d.place_ioc(tk, "buy", action.amount, self.cfg.hedge.slippage_pct)
            self._book(res, "buy")
        elif action.amount > 0 and tk is not None and action.kind == "sell":
            res = d.place_ioc(tk, "sell", action.amount, self.cfg.hedge.slippage_pct)
            self._book(res, "sell")

    def _book(self, res: dict, side: str) -> None:
        if res.get("dry_run"):
            return
        notional = res.get("filled", 0) * res.get("avg_price", 0)
        key = "premium_paid_usd" if side == "buy" else "premium_received_usd"
        self.state[key] = self.state.get(key, 0.0) + notional
        self._save_state()

    def _close_all_hedges(self) -> None:
        d = self.derive
        for pos in d.get_positions(self.cfg.derive.currency):
            if pos.amount > 0:
                tk = d.get_ticker(pos.instrument_name)
                self._book(d.place_ioc(tk, "sell", pos.amount, self.cfg.hedge.slippage_pct), "sell")
