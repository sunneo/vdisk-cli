"""Map wire-level method names to a live :class:`~vdi.fsops.FilesystemOps`.

Shared by the JSON-RPC daemon and the MCP frontend so both expose identical
semantics. All mutation goes through a single lock (libguestfs/guestfish handles
are not thread-safe, and single-writer is the concurrency model -- DESIGN.md 8).
"""
from __future__ import annotations

import base64
import threading

from vdi.fsops import FilesystemOps


class FsService:
    def __init__(self, ops: FilesystemOps, *, readonly: bool = False):
        self.ops = ops
        self.readonly = readonly
        self._lock = threading.RLock()

    # -- dispatch -----------------------------------------------------
    def dispatch(self, method: str, params: dict):
        fn = getattr(self, "_m_" + method.replace(".", "_"), None)
        if fn is None:
            from vdi.errors import VdiError
            raise VdiError(f"unknown method: {method}", data={"code": -32601})
        with self._lock:
            return fn(**params)

    # -- methods ----------------------------------------------------
    def _m_ping(self):
        return {"ok": True, "fs_type": self.ops.fs_type(), "readonly": self.readonly}

    def _m_session_info(self):
        df = self.ops.df()
        return {"fs_type": df.fs_type, "readonly": self.readonly, "df": df.dict()}

    def _m_fs_df(self):
        return self.ops.df().dict()

    def _m_fs_ls(self, path="/", long=False, recursive=False):
        return [e.dict() for e in self.ops.ls(path, long=long, recursive=recursive)]

    def _m_fs_stat(self, path):
        return self.ops.stat(path).dict()

    def _m_fs_size(self, path, apparent=False):
        return {"bytes": self.ops.tree_size(path, apparent=apparent)}

    def _m_fs_read(self, path, offset=0, length=None):
        data = self.ops.read(path, offset, length)
        return {"encoding": "base64", "data": base64.b64encode(data).decode(),
                "length": len(data)}

    def _m_fs_write(self, path, data, encoding="base64", offset=0, append=False):
        raw = base64.b64decode(data) if encoding == "base64" else data.encode()
        self.ops.write(path, raw, offset, append=append)
        return {"written": len(raw)}

    def _m_fs_mkdir(self, path, parents=False):
        self.ops.mkdir(path, parents=parents)
        return {"ok": True}

    def _m_fs_rmdir(self, path, recursive=False):
        self.ops.rmdir(path, recursive=recursive)
        return {"ok": True}

    def _m_fs_rm(self, path):
        self.ops.rm(path)
        return {"ok": True}

    def _m_fs_rename(self, src, dst):
        self.ops.rename(src, dst)
        return {"ok": True}

    def _m_fs_chmod(self, path, mode):
        self.ops.chmod(path, int(str(mode), 8) if isinstance(mode, str) else mode)
        return {"ok": True}

    def _m_fs_chown(self, path, uid, gid):
        self.ops.chown(path, uid, gid)
        return {"ok": True}

    def _m_fs_copyIn(self, host_path, dst):
        self.ops.upload_tree(host_path, dst)
        return {"ok": True}

    def _m_fs_copyOut(self, src, host_path):
        self.ops.download_tree(src, host_path)
        return {"ok": True}

    def _m_fs_grep(self, pattern, path="/", glob=None, ignore_case=False, max_results=1000):
        hits = self.ops.grep(pattern, path, glob=glob, ignore_case=ignore_case,
                             max_results=max_results)
        return [h.dict() for h in hits]
