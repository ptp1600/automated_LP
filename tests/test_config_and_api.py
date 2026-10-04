from fastapi.testclient import TestClient

from lp_hedger.config import Config


def test_config_roundtrip_and_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_HEDGER_DATA", str(tmp_path))
    cfg = Config()
    cfg.update({"lp": {"deploy_usd": "2500", "auto_rebalance": "true"}, "engine": {"autostart": False},
                "chain": {"chain": "ethereum"}, "bogus": {"x": 1},
                "derive": {"subaccount_id": "42"}})          # obsolete key from the execution-era config: ignored
    assert cfg.lp.deploy_usd == 2500.0 and cfg.lp.auto_rebalance is True
    assert cfg.engine.autostart is False and cfg.chain.chain == "ethereum"
    assert not hasattr(cfg.derive, "subaccount_id")
    cfg.save()
    again = Config.load()
    assert again.to_dict() == cfg.to_dict()


def test_api_flow():
    from lp_hedger import server

    server.engine.stop()
    c = TestClient(server.app)
    assert c.get("/").status_code == 200
    st = c.get("/api/status").json()
    assert st["paper"] is True and st["running"] is False
    keys = [p["key"] for p in c.get("/api/presets").json()]
    assert keys == ["ethereum", "arbitrum", "base"]
    assert all(len(p["pools"]) >= 2 for p in c.get("/api/presets").json())

    r = c.put("/api/config", json={"hedge": {"coverage_pct": 80}, "lp": {"deploy_usd": 5000}})
    assert r.status_code == 200 and r.json()["config"]["hedge"]["coverage_pct"] == 80
    assert c.put("/api/config", json={"lp": {"deploy_usd": "lots"}}).status_code == 400
    assert c.post("/api/paper/open_lp").status_code == 400       # engine not running
    assert c.post("/api/paper/nope").status_code == 404
    assert c.post("/api/paper/close_lp").status_code == 400
    # no wallet endpoints exist any more
    assert c.post("/api/wallet/create", json={"password": "x" * 10}).status_code in (404, 405)
