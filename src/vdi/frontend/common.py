"""Shared helpers for the host-side frontends."""
from __future__ import annotations

import contextlib
import threading

from vdi.errors import NotFound
from vdi.target import normalize_inner


class FsView:
    """A thin, locked, path-normalising wrapper over a FilesystemOps.

    Frontends work in terms of POSIX paths rooted at ``/`` (optionally jailed to
    ``root``). All access goes through ``lock`` (the daemon's single-writer lock).
    """

    def __init__(self, ops, *, readonly: bool = False, root: str = "/", lock=None):
        self.ops = ops
        self.readonly = readonly
        self.root = (root or "/").rstrip("/")
        self._lock = lock or contextlib.nullcontext()

    # -- path handling -------------------------------------------
    def real(self, path: str) -> str:
        p = normalize_inner((self.root + "/" + path) if not path.startswith("/")
                            else self.root + path)
        if self.root and not (p == self.root or p.startswith(self.root + "/")):
            raise NotFound(path)
        return p or "/"

    # -- locked operations --------------------------------------
    def listdir(self, path):
        with self._lock:
            return self.ops.ls(self.real(path), long=True)

    def stat(self, path):
        with self._lock:
            return self.ops.stat(self.real(path))

    def read(self, path, offset=0, length=None):
        with self._lock:
            return self.ops.read(self.real(path), offset, length)

    def write(self, path, data, *, append=False):
        self._guard()
        with self._lock:
            self.ops.write(self.real(path), data, append=append)

    def mkdir(self, path):
        self._guard()
        with self._lock:
            self.ops.mkdir(self.real(path), parents=True)

    def rmdir(self, path):
        self._guard()
        with self._lock:
            self.ops.rmdir(self.real(path), recursive=True)

    def remove(self, path):
        self._guard()
        with self._lock:
            self.ops.rm(self.real(path))

    def rename(self, src, dst):
        self._guard()
        with self._lock:
            self.ops.rename(self.real(src), self.real(dst))

    def exists(self, path) -> bool:
        try:
            self.stat(path)
            return True
        except Exception:
            return False

    def is_dir(self, path) -> bool:
        try:
            return self.stat(path).type == "dir"
        except Exception:
            return False

    def _guard(self):
        if self.readonly:
            from vdi.errors import ReadOnly
            raise ReadOnly()


def serve_bg(server) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
