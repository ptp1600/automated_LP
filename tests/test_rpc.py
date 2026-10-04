import json

import pytest

from lp_hedger.rpc import ArchiveUnsupported, JsonRpc, LogRangeTooWide, RpcError, classify


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


class FakeSession:
    """Answers eth_blockNumber / eth_call; rejects batches larger than ``max_batch``."""

    def __init__(self, max_batch=3, batch_supported=True):
        self.max_batch = max_batch
        self.batch_supported = batch_supported
        self.requests = []

    def _one(self, req):
        m = req["method"]
        if m == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": req["id"], "result": "0x10"}
        if m == "eth_call":
            blk = req["params"][1]
            if blk != "latest" and int(blk, 16) < 5:
                return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32000, "message": "missing trie node"}}
            return {"jsonrpc": "2.0", "id": req["id"], "result": "0x" + "00" * 31 + "2a"}
        return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "method not found"}}

    def post(self, url, json=None, timeout=None, headers=None):
        self.requests.append(json)
        if isinstance(json, list):
            if not self.batch_supported:
                return FakeResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batch not supported"}})
            if len(json) > self.max_batch:
                return FakeResponse([{"jsonrpc": "2.0", "id": r["id"], "error": {"code": 3, "message": "Batch of more than 3 requests are not allowed"}} for r in json])
            return FakeResponse([self._one(r) for r in json])
        return FakeResponse(self._one(json))


def test_batch_is_chunked_to_node_limit():
    s = FakeSession(max_batch=3)
    rpc = JsonRpc("http://fake", session=s)
    res = rpc.batch([("eth_blockNumber", [])] * 7)
    assert res == ["0x10"] * 7
    sizes = [len(r) for r in s.requests if isinstance(r, list)]
    assert sizes == [3, 3] and sum(1 for r in s.requests if isinstance(r, dict)) == 1
    assert rpc.batch_ok


def test_batch_falls_back_to_sequential_when_rejected():
    s = FakeSession(batch_supported=False)
    rpc = JsonRpc("http://fake", session=s)
    assert rpc.batch([("eth_blockNumber", [])] * 4) == ["0x10"] * 4
    assert rpc.batch_ok is False
    rpc.batch([("eth_blockNumber", [])] * 2)
    assert all(isinstance(r, dict) for r in s.requests[1:])


def test_eth_call_decodes_and_classifies_archive_errors():
    rpc = JsonRpc("http://fake", session=FakeSession())
    (liq,) = rpc.eth_call("0x" + "11" * 20, "liquidity", [])
    assert liq == 42
    with pytest.raises(ArchiveUnsupported):
        rpc.eth_call("0x" + "11" * 20, "liquidity", [], block=3)
    with pytest.raises(RpcError):
        rpc.call("eth_nope", [])


def test_classify_messages():
    assert isinstance(classify(RpcError("eth_getLogs is limited to a 2,000 range"), logs=True), LogRangeTooWide)
    assert isinstance(classify(RpcError("Archive requests require a personal token", code=-32602), logs=True), ArchiveUnsupported)
    assert isinstance(classify(RpcError("Unknown state. First available state is 1", code=27), historical=True), ArchiveUnsupported)
    assert type(classify(RpcError("boom"), historical=False)) is RpcError
