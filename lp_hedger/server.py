"""Local web server: serves the UI and a small JSON API bound to 127.0.0.1."""
from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .chains import presets_for_ui
from .config import Config
from .engine import Engine
from .wallet import HotWallet

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="LP Hedger", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

cfg = Config.load()
wallet = HotWallet()
engine = Engine(cfg, wallet)


class WalletReq(BaseModel):
    password: str
    private_key: Optional[str] = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return engine.status()


@app.get("/api/presets")
def presets():
    return presets_for_ui()


@app.get("/api/config")
def get_config():
    return {"config": cfg.to_dict()}


@app.put("/api/config")
def put_config(patch: dict[str, Any]):
    try:
        cfg.update(patch)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"invalid setting: {e}")
    cfg.save()
    engine.rebuild_clients()
    return {"config": cfg.to_dict()}


@app.post("/api/wallet/create")
def wallet_create(req: WalletReq):
    try:
        addr = wallet.create(req.password, req.private_key or None)
    except (FileExistsError, ValueError) as e:
        raise HTTPException(400, str(e))
    engine.log("info", f"hot wallet ready: {addr}")
    engine.rebuild_clients()
    return {"address": addr}


@app.post("/api/wallet/unlock")
def wallet_unlock(req: WalletReq):
    try:
        addr = wallet.unlock(req.password)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    engine.rebuild_clients()
    if cfg.engine.autostart and not engine.running:
        engine.start()
    return {"address": addr}


@app.post("/api/wallet/lock")
def wallet_lock():
    engine.stop()
    wallet.lock()
    return {"ok": True}


@app.post("/api/engine/start")
def engine_start():
    try:
        engine.start()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"running": engine.running}


@app.post("/api/engine/stop")
def engine_stop():
    engine.stop()
    return {"running": engine.running}


@app.post("/api/jobs/{job}")
def enqueue(job: str):
    if job not in {"open_lp", "close_lp", "collect_fees", "rebalance", "hedge_now", "close_hedges"}:
        raise HTTPException(404, "unknown job")
    if not engine.running:
        raise HTTPException(400, "Start the engine first")
    engine.enqueue(job)
    return {"queued": job}


@app.post("/api/derive/verify")
def derive_verify():
    if not wallet.unlocked:
        raise HTTPException(400, "Unlock the wallet first")
    if not engine.derive_configured():
        raise HTTPException(400, "Fill in the Derive wallet address and subaccount ID first")
    engine.rebuild_clients()
    try:
        return engine.derive.verify_connection()
    except Exception as e:
        raise HTTPException(400, f"{e}")


def main() -> None:
    p = argparse.ArgumentParser(description="LP Hedger: hedged Uniswap v3 LP automation")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep local!)")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"LP Hedger UI: {url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
