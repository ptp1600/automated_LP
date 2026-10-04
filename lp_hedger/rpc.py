"""Minimal JSON-RPC client for Ethereum-style nodes.

Only what the paper trader needs: ``eth_call`` (optionally at a historical
block), block headers, logs, gas price, with request batching and a retry on
rate limits. Errors that mean "this node will never answer that" (no archive
state, log range too wide) are classified so callers can switch strategy
instead of retrying blindly.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests
from eth_abi.abi import decode, encode

from . import abis

RETRY_STATUS = {429, 502, 503, 504}


class RpcError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class ArchiveUnsupported(RpcError):
    """The node has no state for the requested historical block."""


class LogRangeTooWide(RpcError):
    """The node refuses eth_getLogs over that many blocks."""


_ARCHIVE_HINTS = ("archive", "missing trie node", "unknown state", "historical", "pruned", "state is not available",
                  "not available for block", "older than", "header not found", "block not found")
_RANGE_HINTS = ("range", "too many blocks", "block range", "exceed", "limited to", "max results", "too large",
                "query returned more than", "response size")


def classify(err: RpcError, historical: bool = False, logs: bool = False) -> RpcError:
    msg = str(err).lower()
    if logs and any(h in msg for h in _RANGE_HINTS):
        return LogRangeTooWide(str(err), err.code, err.status)
    if (historical or logs) and (any(h in msg for h in _ARCHIVE_HINTS) or err.code in (-32000, -32602, 27)):
        return ArchiveUnsupported(str(err), err.code, err.status)
    return err


class JsonRpc:
    def __init__(self, url: str, timeout: float = 30, session: Optional[requests.Session] = None,
                 retries: int = 3, max_batch: int = 3):
        self.url = url
        self.timeout = timeout
        self.http = session or requests.Session()
        self.retries = retries
        self.batch_ok = True
        self.max_batch = max_batch        # free tiers of public nodes often cap batches at 3
        self._id = 0

    # ---- transport -----------------------------------------------------------
    def _post(self, payload: Any) -> Any:
        delay = 0.6
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(self.url, json=payload, timeout=self.timeout,
                                   headers={"content-type": "application/json", "accept": "application/json"})
            except requests.RequestException as e:
                last = RpcError(f"{self.url}: {e}")
            else:
                if r.status_code in RETRY_STATUS and attempt < self.retries:
                    last = RpcError(f"{self.url}: HTTP {r.status_code}", status=r.status_code)
                else:
                    try:
                        body = r.json()
                    except ValueError:
                        raise RpcError(f"{self.url}: HTTP {r.status_code} non-JSON response", status=r.status_code)
                    if r.status_code >= 400 and not isinstance(body, (dict, list)):
                        raise RpcError(f"{self.url}: HTTP {r.status_code}", status=r.status_code)
                    return body
            time.sleep(delay)
            delay *= 2
        raise last or RpcError(f"{self.url}: request failed")

    @staticmethod
    def _unwrap(body: Any) -> Any:
        if not isinstance(body, dict):
            raise RpcError(f"unexpected response: {str(body)[:120]}")
        if body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                raise RpcError(str(err.get("message") or err), code=err.get("code"))
            raise RpcError(str(err))
        return body.get("result")

    def call(self, method: str, params: list) -> Any:
        self._id += 1
        return self._unwrap(self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}))

    def batch(self, calls: list[tuple[str, list]]) -> list[Any]:
        """Run several calls in as few HTTP requests as the node allows; falls back
        to sequential calls when it rejects batches. Raises the first error."""
        if not calls:
            return []
        if self.batch_ok and len(calls) > 1:
            out: list[Any] = []
            for i in range(0, len(calls), self.max_batch):
                chunk = calls[i:i + self.max_batch]
                if len(chunk) == 1:
                    out.append(self.call(*chunk[0]))
                    continue
                res = self._batch_once(chunk)
                if res is None:
                    self.batch_ok = False      # node does not like batches; remember that
                    break
                out.extend(res)
            else:
                return out
        return [self.call(m, p) for m, p in calls]

    def _batch_once(self, calls: list[tuple[str, list]]) -> Optional[list[Any]]:
        payload = []
        for m, p in calls:
            self._id += 1
            payload.append({"jsonrpc": "2.0", "id": self._id, "method": m, "params": p})
        try:
            body = self._post(payload)
        except RpcError:
            return None
        if not isinstance(body, list) or len(body) != len(payload):
            return None
        by_id = {b.get("id"): b for b in body if isinstance(b, dict)}
        if len(by_id) != len(payload):
            return None
        results = []
        for req in payload:
            try:
                results.append(self._unwrap(by_id[req["id"]]))
            except RpcError as e:
                if "batch" in str(e).lower():
                    return None            # "batches not allowed": retry sequentially
                raise
        return results

    # ---- helpers -------------------------------------------------------------
    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_block(self, number: "int | str") -> dict:
        tag = hex(number) if isinstance(number, int) else number
        try:
            blk = self.call("eth_getBlockByNumber", [tag, False])
        except RpcError as e:
            raise classify(e, historical=isinstance(number, int)) from e
        if not blk:
            raise ArchiveUnsupported(f"block {tag} not available on {self.url}")
        return {"number": int(blk["number"], 16), "timestamp": int(blk["timestamp"], 16),
                "base_fee": int(blk.get("baseFeePerGas") or "0x0", 16)}

    def gas_price(self) -> int:
        return int(self.call("eth_gasPrice", []), 16)

    @staticmethod
    def encode_call(fn: str, args: list) -> str:
        sig, in_types, _ = abis.FUNCTIONS[fn]
        data = abis.SELECTORS[fn]
        if in_types:
            data += encode(in_types, args).hex()
        return data

    @staticmethod
    def decode_result(fn: str, raw: str) -> tuple:
        _, _, out_types = abis.FUNCTIONS[fn]
        if not raw or raw == "0x":
            raise RpcError(f"{fn}: empty return data")
        return decode(out_types, bytes.fromhex(raw[2:]))

    def eth_call(self, to: str, fn: str, args: list, block: "int | str" = "latest") -> tuple:
        tag = hex(block) if isinstance(block, int) else block
        try:
            raw = self.call("eth_call", [{"to": to, "data": self.encode_call(fn, args)}, tag])
        except RpcError as e:
            raise classify(e, historical=isinstance(block, int)) from e
        return self.decode_result(fn, raw)

    def eth_calls(self, to: str, fns: list[tuple[str, list]], block: "int | str" = "latest") -> list[tuple]:
        """Several eth_calls against one contract at one block, batched."""
        tag = hex(block) if isinstance(block, int) else block
        try:
            raws = self.batch([("eth_call", [{"to": to, "data": self.encode_call(fn, args)}, tag]) for fn, args in fns])
        except RpcError as e:
            raise classify(e, historical=isinstance(block, int)) from e
        return [self.decode_result(fn, raw) for (fn, _), raw in zip(fns, raws)]

    def get_logs(self, address: str, topics: list[str], from_block: int, to_block: int) -> list[dict]:
        try:
            return self.call("eth_getLogs", [{"address": address, "topics": topics,
                                              "fromBlock": hex(from_block), "toBlock": hex(to_block)}]) or []
        except RpcError as e:
            raise classify(e, historical=True, logs=True) from e
