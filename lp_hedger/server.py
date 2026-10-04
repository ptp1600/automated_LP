"""Local web server: serves the paper-trading UI and a small JSON API on 127.0.0.1."""
from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .chains import presets_for_ui
from .config import Config
from .engine import Engine

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="LP Hedger (paper)", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

cfg = Config.load()
engine = Engine(cfg)

JOBS = {"open_lp", "close_lp", "rebalance", "hedge_now", "close_hedges", "reset", "refresh_history"}


@app.on_event("startup")
def _autostart() -> None:
    if cfg.engine.autostart:
        engine.start()


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


@app.post("/api/engine/start")
def engine_start():
    engine.start()
    return {"running": engine.running}


@app.post("/api/engine/stop")
def engine_stop():
    engine.stop()
    return {"running": engine.running}


@app.post("/api/paper/{job}")
def enqueue(job: str):
    if job not in JOBS:
        raise HTTPException(404, "unknown job")
    if not engine.running:
        raise HTTPException(400, "Start the engine first")
    if job == "open_lp" and engine.lp is not None:
        raise HTTPException(400, "A paper LP position is already open")
    if job in ("close_lp", "rebalance") and engine.lp is None:
        raise HTTPException(400, "No paper LP position is open")
    engine.enqueue(job)
    return {"queued": job}


def main() -> None:
    p = argparse.ArgumentParser(description="LP Hedger: paper-trade hedged Uniswap v3 LP positions")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep local!)")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"LP Hedger paper trading UI: {url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
