"""JSON-RPC 2.0 over loopback TCP with a per-session token and codec negotiation.

Channel setup (always JSON):
    ->  {"hello":{"v":1,"codecs":["toon","json"]}}
    <-  {"hello":{"v":1,"codec":"<chosen>"}}

After that, every message is one <codec>-encoded object per line. A message MAY
be followed by a raw binary payload: if it carries ``"raw": N`` then exactly N
raw bytes follow the newline (both directions). That is the data channel for
fs.read / fs.write / cp-in / cp-out -- file bytes never pay an encoding tax.

The client keeps one persistent connection and transparently reconnects.
"""
from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading

from vdi.errors import VdiError, from_rpc
from vdi.wire import Codec, PROTOCOL_VERSION, DEFAULT_ORDER, pick


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _recv_line(sock, buf: bytearray):
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\n")
    return line, bytearray(rest)


def _recv_exact(sock, buf: bytearray, n: int):
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf[:n]), bytearray(buf[n:])


class RpcClient:
    def __init__(self, addr, port, token, timeout=120.0, codecs=DEFAULT_ORDER):
        self.addr, self.port, self.token, self.timeout = addr, port, token, timeout
        self._want = list(codecs)
        self._id = 0
        self._sock = None
        self._buf = bytearray()
        self.codec = Codec("json")
        self._lock = threading.Lock()

    def _connect(self):
        s = socket.create_connection((self.addr, self.port), timeout=self.timeout)
        s.sendall((json.dumps({"hello": {"v": PROTOCOL_VERSION, "codecs": self._want}}) + "\n").encode())
        buf = bytearray()
        line, buf = _recv_line(s, buf)
        hello = json.loads(line.decode()).get("hello", {})
        self.codec = Codec(hello.get("codec", "json"))
        self._sock, self._buf = s, buf

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def call(self, method, *, _raw_out: bytes | None = None, _want_raw: bool = False, **params):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    return self._do(method, params, _raw_out, _want_raw)
                except (OSError, ValueError):
                    self.close()
                    if attempt == 2:
                        raise

    def _do(self, method, params, raw_out, want_raw):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method,
               "params": {"token": self.token, **params}}
        if raw_out is not None:
            req["raw"] = len(raw_out)
        self._sock.sendall(self.codec.encode(req) + b"\n" + (raw_out or b""))
        line, self._buf = _recv_line(self._sock, self._buf)
        resp = self.codec.decode(line)
        if resp.get("error"):
            raise from_rpc(resp["error"])
        if resp.get("raw"):
            blob, self._buf = _recv_exact(self._sock, self._buf, int(resp["raw"]))
            return resp.get("result"), blob
        if want_raw:
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
                try:
                    hello = json.loads(line.decode()).get("hello", {})
                except Exception:
                    return
                codec = Codec(pick(hello.get("codecs", ["json"])))
                self.request.sendall(
                    (json.dumps({"hello": {"v": PROTOCOL_VERSION, "codec": codec.name}}) + "\n").encode())

                while True:
                    line, buf = _recv_line(self.request, buf)
                    if not line:
                        break
                    try:
                        req = codec.decode(line)
                    except Exception:
                        self.request.sendall(codec.encode(
                            {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}}) + b"\n")
                        continue
                    raw_in = b""
                    if req.get("raw"):
                        raw_in, buf = _recv_exact(self.request, buf, int(req["raw"]))
                    self._respond(req, raw_in, codec)

            def _respond(self, req, raw_in, codec):
                rid = req.get("id")
                params = req.get("params") or {}
                if params.get("token") != outer.token:
                    self.request.sendall(codec.encode(
                        {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32006, "message": "bad token"}}) + b"\n")
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
                    self.request.sendall(codec.encode(msg) + b"\n" + blob)
                except VdiError as e:
                    self.request.sendall(codec.encode(
                        {"jsonrpc": "2.0", "id": rid, "error": e.to_rpc()}) + b"\n")
                except Exception as e:  # pragma: no cover
                    self.request.sendall(codec.encode(
                        {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32000, "message": repr(e)}}) + b"\n")

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
