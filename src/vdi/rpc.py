"""JSON-RPC 2.0 over loopback TCP with a per-session bearer token.

Framing: one JSON object per line. A message MAY be followed by a raw binary
payload -- if the JSON carries ``"raw": N`` then exactly N bytes follow the
newline. This is the data channel for read/write/cp-in/cp-out (no base64).
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


def _recv_line(sock, buf: bytearray) -> tuple[bytes, bytearray]:
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\n")
    return line, bytearray(rest)


def _recv_exact(sock, buf: bytearray, n: int) -> tuple[bytes, bytearray]:
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf[:n]), bytearray(buf[n:])


class RpcClient:
    def __init__(self, addr, port, token, timeout=120.0):
        self.addr, self.port, self.token, self.timeout = addr, port, token, timeout
        self._id = 0

    def call(self, method, *, _raw_out: bytes | None = None,
             _want_raw: bool = False, **params):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": {"token": self.token, **params}}
        if _raw_out is not None:
            req["raw"] = len(_raw_out)
        with socket.create_connection((self.addr, self.port), timeout=self.timeout) as sock:
            sock.sendall((json.dumps(req) + "\n").encode())
            if _raw_out is not None:
                sock.sendall(_raw_out)
            buf = bytearray()
            line, buf = _recv_line(sock, buf)
            resp = json.loads(line.decode())
            if "error" in resp:
                raise from_rpc(resp["error"])
            if resp.get("raw"):
                blob, buf = _recv_exact(sock, buf, int(resp["raw"]))
                return resp.get("result"), blob
            if _want_raw:
                return resp.get("result"), b""
            return resp.get("result")


class RpcServer:
    def __init__(self, dispatch, token, addr="127.0.0.1", port=0):
        self.dispatch = dispatch
        self.token = token
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                buf = bytearray()
                line, buf = _recv_line(self.request, buf)
                if not line.strip():
                    return
                try:
                    req = json.loads(line.decode())
                except json.JSONDecodeError:
                    self._send({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "parse error"}})
                    return
                raw_in = b""
                if req.get("raw"):
                    raw_in, buf = _recv_exact(self.request, buf, int(req["raw"]))
                rid = req.get("id")
                params = req.get("params") or {}
                if params.get("token") != outer.token:
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32006, "message": "bad token"}})
                    return
                params = {k: v for k, v in params.items() if k != "token"}
                if raw_in:
                    params["_raw"] = raw_in
                try:
                    result = outer.dispatch(req.get("method", ""), params)
                    blob = b""
                    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], (bytes, bytearray)):
                        result, blob = result[0], bytes(result[1])
                    msg = {"jsonrpc": "2.0", "id": rid, "result": result}
                    if blob:
                        msg["raw"] = len(blob)
                    self._send(msg, blob)
                except VdiError as e:
                    self._send({"jsonrpc": "2.0", "id": rid, "error": e.to_rpc()})
                except Exception as e:  # pragma: no cover
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32000, "message": repr(e)}})

            def _send(self, obj, blob: bytes = b""):
                self.request.sendall((json.dumps(obj) + "\n").encode() + blob)

        self._srv = socketserver.ThreadingTCPServer((addr, port), Handler)
        self._srv.daemon_threads = True
        self.addr, self.port = self._srv.server_address

    def serve_forever(self):
        self._srv.serve_forever()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self):
        self._srv.shutdown()
