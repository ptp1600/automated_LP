import pytest
from fastapi.testclient import TestClient

from lp_hedger.config import Config


def test_config_roundtrip_and_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_HEDGER_DATA", str(tmp_path))
    cfg = Config()
    cfg.update({"lp": {"deploy_usd": "2500", "auto_rebalance": "true"}, "engine": {"dry_run": False},
                "derive": {"subaccount_id": "42"}, "bogus": {"x": 1}})
    assert cfg.lp.deploy_usd == 2500.0 and cfg.lp.auto_rebalance is True
    assert cfg.engine.dry_run is False and cfg.derive.subaccount_id == 42
    cfg.save()
    again = Config.load()
    assert again.to_dict() == cfg.to_dict()


def test_api_flow():
    from lp_hedger import server

    c = TestClient(server.app)
    assert c.get("/").status_code == 200
    st = c.get("/api/status").json()
    assert st["wallet_exists"] is False and st["running"] is False
    assert c.get("/api/presets").json()[0]["key"] == "arbitrum"

    r = c.post("/api/wallet/create", json={"password": "short"})
    assert r.status_code == 400
    r = c.post("/api/wallet/create", json={"password": "correct horse battery"})
    assert r.status_code == 200 and r.json()["address"].startswith("0x")
    addr = r.json()["address"]
    assert c.post("/api/wallet/create", json={"password": "correct horse battery"}).status_code == 400

    c.post("/api/wallet/lock")
    assert c.get("/api/status").json()["wallet_unlocked"] is False
    assert c.post("/api/wallet/unlock", json={"password": "wrong password!"}).status_code == 400
    assert c.post("/api/wallet/unlock", json={"password": "correct horse battery"}).json()["address"] == addr

    r = c.put("/api/config", json={"hedge": {"coverage_pct": 80}, "derive": {"derive_wallet": "0x" + "ab" * 20, "subaccount_id": 9}})
    assert r.status_code == 200 and r.json()["config"]["hedge"]["coverage_pct"] == 80
    assert c.post("/api/jobs/open_lp").status_code == 400       # engine not running
    assert c.post("/api/jobs/nope").status_code == 404
    assert c.get("/api/status").json()["derive_configured"] is True
    assert c.get("/api/status").json()["dry_run"] is True
