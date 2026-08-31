"""``vdi serve`` -- hold an image open and expose it over JSON-RPC (+ optional MCP)."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

from vdi import registry
from vdi.engine import get_engine
from vdi.rpc import RpcServer, new_token
from vdi.service import FsService


class Daemon:
    def __init__(self, image: str, *, name: str | None, partition: str | None,
                 readonly: bool, engine: str, rpc_addr: str, rpc_port: int,
                 idle_timeout: float | None = None, mcp: str | None = None,
                 mcp_writable: bool = False, mcp_root: str = "/"):
        self.image = os.path.abspath(image)
        raw_name = name or os.path.splitext(os.path.basename(self.image))[0]
        self.name = _sanitize_name(raw_name)
        self.partition = partition
        self.readonly = readonly
        self.engine_name = engine
        self.rpc_addr, self.rpc_port = rpc_addr, rpc_port
        self.idle_timeout = idle_timeout
        self.mcp = mcp
        self.mcp_writable = mcp_writable
        self.mcp_root = mcp_root
        self._stop = threading.Event()
        self._last_activity = time.time()

    def run(self) -> int:
        engine = get_engine(self.engine_name)
        info = engine.probe()
        if not info.available:
            print(f"engine {engine.name!r} not available: {info.detail}", file=sys.stderr)
            return 2

        print(f"[vdi] opening {self.image} via {engine.name} engine ...", file=sys.stderr)
        with engine.open_image(self.image, self.partition, readonly=self.readonly) as img:
            service = FsService(img, readonly=self.readonly)
            token = new_token()

            def dispatch(method, params):
                self._last_activity = time.time()
                return service.dispatch(method, params)

            server = RpcServer(dispatch, token, self.rpc_addr, self.rpc_port)

            sess = registry.Session(
                name=self.name, pid=os.getpid(), image=self.image,
                image_format=_fmt(self.image), partition=self.partition,
                fs_type=img.fs_type(), readonly=self.readonly, engine=engine.name,
                rpc=registry.RpcEndpoint(addr=server.addr, port=server.port, token=token),
            )
            try:
                registry.create(sess)
            except registry.RegistryConflict as e:
                print(f"[vdi] {e}", file=sys.stderr)
                return 3

            print(f"[vdi] session {self.name!r} ready  rpc={server.addr}:{server.port}  fs={img.fs_type()}",
                  file=sys.stderr)
            server.start_thread()

            mcp_srv = None
            if self.mcp and self.mcp != "stdio":
                from vdi.mcp_server import McpTools, serve_tcp_shim
                mhost, _, mport = self.mcp.rpartition(":")
                tools = McpTools(img, writable=self.mcp_writable, root=self.mcp_root,
                                 lock=service._lock)
                mcp_srv = serve_tcp_shim(tools, mhost or "127.0.0.1", int(mport))
                threading.Thread(target=mcp_srv.serve_forever, daemon=True).start()
                sess.mounts["mcp"] = f"{mcp_srv.server_address[0]}:{mcp_srv.server_address[1]}"
                sess.path().write_text(sess.to_json())
                print(f"[vdi] mcp (shim) on {sess.mounts['mcp']}  writable={self.mcp_writable}",
                      file=sys.stderr)

            self._install_signals()
            try:
                while not self._stop.is_set():
                    self._stop.wait(2.0)
                    if self.idle_timeout and (time.time() - self._last_activity) > self.idle_timeout:
                        print("[vdi] idle timeout, shutting down", file=sys.stderr)
                        break
            finally:
                server.shutdown()
                if mcp_srv is not None:
                    mcp_srv.shutdown()
                registry.remove(self.name)
        print("[vdi] stopped", file=sys.stderr)
        return 0

    def _install_signals(self):
        def handler(signum, frame):
            self._stop.set()
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handler)
            except (ValueError, OSError):
                pass


def _sanitize_name(raw: str) -> str:
    import re
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return cleaned or "session"


def _fmt(path: str) -> str:
    from vdi.image import fmt_from_path
    try:
        return fmt_from_path(path)
    except Exception:
        return "unknown"
