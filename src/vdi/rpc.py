"""Minimal JSON-RPC 2.0 over loopback TCP with a per-session bearer token.

Wire framing: one JSON object per line (newline-delimited). Binary payloads for
read/write are base64 in ``params``/``result`` for now; a raw data channel for
cp-in/cp-out is a TODO (see DESIGN.md 5.3).
"""
from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading

from vdi.errors import VdiError, from_rpc


def new_token() -> str:
    return secrets.token_urlsafe(24)


# -- client --------------------------------------------------------------
class RpcClient:
    def __init__(self, addr: str, port: int, token: str, timeout: float = 30.0):
        self.addr, self.port, self.token, self.timeout = addr, port, token, timeout
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": {"token": self.token, **params}}
        with socket.create_connection((self.addr, self.port), timeout=self.timeout) as sock:
            sock.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode())
        if "error" in resp:
            raise from_rpc(resp["error"])
        return resp.get("result")


# -- server --------------------------------------------------------------
class RpcServer:
    def __init__(self, dispatch, token: str, addr: str = "127.0.0.1", port: int = 0):
        self.dispatch = dispatch
        self.token = token
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    if not line.strip():
                        continue
                    self.wfile.write(outer._handle_line(line).encode() + b"\n")

        self._srv = socketserver.ThreadingTCPServer((addr, port), Handler, bind_and_activate=True)
        self._srv.daemon_threads = True
        self.addr, self.port = self._srv.server_address

    def _handle_line(self, line: bytes) -> str:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32700, "message": "parse error"}})
        rid = req.get("id")
        params = req.get("params") or {}
        if params.get("token") != self.token:
            return json.dumps({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32006, "message": "bad token"}})
        params = {k: v for k, v in params.items() if k != "token"}
        try:
            result = self.dispatch(req.get("method", ""), params)
            return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
        except VdiError as e:
            return json.dumps({"jsonrpc": "2.0", "id": rid, "error": e.to_rpc()})
        except Exception as e:  # pragma: no cover - defensive
            return json.dumps({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32000, "message": repr(e)}})

    def serve_forever(self):
        self._srv.serve_forever()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self):
        self._srv.shutdown()
