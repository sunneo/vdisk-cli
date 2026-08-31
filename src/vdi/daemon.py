"""``vdi serve`` -- hold an image open and expose it over JSON-RPC + optional
MCP / FTP / WebDAV / FUSE frontends. All frontends share the single-writer lock.
"""
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


def _split_bind(s: str, default_host="127.0.0.1", default_port=0):
    if s is None:
        return None
    if ":" in s:
        h, _, p = s.rpartition(":")
        return (h or default_host, int(p or default_port))
    if s.isdigit():
        return (default_host, int(s))
    return (s, default_port)


class Daemon:
    def __init__(self, image, *, name=None, partition=None, readonly=False,
                 engine="auto", rpc_addr="127.0.0.1", rpc_port=0, idle_timeout=None,
                 mcp=None, mcp_writable=False, mcp_root="/",
                 ftp=None, webdav=None, mount=None):
        self.image = os.path.abspath(image)
        self.name = _sanitize_name(name or os.path.splitext(os.path.basename(self.image))[0])
        self.partition = partition
        self.readonly = readonly
        self.engine_name = engine
        self.rpc_addr, self.rpc_port = rpc_addr, rpc_port
        self.idle_timeout = idle_timeout
        self.mcp, self.mcp_writable, self.mcp_root = mcp, mcp_writable, mcp_root
        self.ftp, self.webdav, self.mount = ftp, webdav, mount
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
            stoppers = []

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

            # -- MCP -------------------------------------------------
            if self.mcp and self.mcp != "stdio":
                from vdi.mcp_server import McpTools, serve_tcp_shim
                h, p = _split_bind(self.mcp, default_port=7333)
                tools = McpTools(img, writable=self.mcp_writable, root=self.mcp_root,
                                 lock=service._lock)
                srv = serve_tcp_shim(tools, h, p)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                stoppers.append(srv.shutdown)
                sess.mounts["mcp"] = f"{srv.server_address[0]}:{srv.server_address[1]}"
                print(f"[vdi] mcp on {sess.mounts['mcp']}  writable={self.mcp_writable}", file=sys.stderr)

            # -- FTP / WebDAV --------------------------------------
            if self.ftp is not None or self.webdav is not None:
                from vdi.frontend.common import FsView

                class _Activity:
                    def __getattr__(_s, n):
                        self._last_activity = time.time()
                        return getattr(img, n)

                view = FsView(_Activity(), readonly=self.readonly, root=self.mcp_root,
                              lock=service._lock)

                if self.ftp is not None:
                    from vdi.frontend.ftp import FtpFrontend
                    h, p = _split_bind(self.ftp, default_port=2121)
                    f = FtpFrontend(view, h, p, token=token)
                    f.start()
                    stoppers.append(f.stop)
                    sess.mounts["ftp"] = f"{f.host}:{f.port}"
                    print(f"[vdi] ftp on ftp://{f.host}:{f.port}  (user: anything, pass: the RPC token)",
                          file=sys.stderr)

                if self.webdav is not None:
                    from vdi.frontend.webdav import WebdavFrontend
                    h, p = _split_bind(self.webdav, default_port=8080)
                    w = WebdavFrontend(view, h, p, token=token)
                    w.start()
                    stoppers.append(w.stop)
                    sess.mounts["webdav"] = f"http://{w.host}:{w.port}"
                    print(f"[vdi] webdav on {sess.mounts['webdav']}  (pass: the RPC token)", file=sys.stderr)

            # -- FUSE ---------------------------------------------
            if self.mount:
                try:
                    from vdi.frontend import start_fuse
                    start_fuse(img, self.mount, readonly=self.readonly, lock=service._lock)
                    sess.mounts["fuse"] = self.mount
                    print(f"[vdi] fuse mounted at {self.mount}", file=sys.stderr)
                except Exception as e:
                    print(f"[vdi] --mount failed: {e}", file=sys.stderr)

            sess.path().write_text(sess.to_json())
            self._install_signals()
            try:
                while not self._stop.is_set():
                    self._stop.wait(2.0)
                    if self.idle_timeout and (time.time() - self._last_activity) > self.idle_timeout:
                        print("[vdi] idle timeout, shutting down", file=sys.stderr)
                        break
            finally:
                for s in stoppers:
                    try:
                        s()
                    except Exception:
                        pass
                server.shutdown()
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
    return re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-") or "session"


def _fmt(path: str) -> str:
    from vdi.image import fmt_from_path
    try:
        return fmt_from_path(path)
    except Exception:
        return "unknown"
