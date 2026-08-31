"""FUSE mount frontend (Linux/macOS). Needs ``fusepy`` (pip install fusepy) and
a libfuse. On Windows this raises with a pointer to ``--webdav`` (which Explorer
can map as a drive) since WinFsp bindings are not bundled.
"""
from __future__ import annotations

import errno
import stat as _stat
import sys
import threading


def start_fuse(ops, mountpoint: str, *, readonly: bool = False, lock=None):
    if sys.platform == "win32":
        raise RuntimeError(
            "FUSE mount is Linux/macOS only. On Windows use --webdav and map it "
            "as a network drive:  net use X: http://127.0.0.1:8080")
    try:
        from fuse import FUSE, FuseOSError, Operations, LoggingMixIn  # type: ignore
    except Exception as e:
        raise RuntimeError(f"fusepy not available ({e}); pip install fusepy") from e

    from vdi.frontend.common import FsView
    view = FsView(ops, readonly=readonly, lock=lock)

    class _VdiFuse(Operations):
        def getattr(self, path, fh=None):
            try:
                st = view.stat(path)
            except Exception:
                raise FuseOSError(errno.ENOENT)
            mode = int(st.mode, 8) if st.mode.isdigit() else 0o644
            typ = _stat.S_IFDIR | 0o755 if st.type == "dir" else _stat.S_IFREG | mode
            return {"st_mode": typ, "st_size": st.size, "st_nlink": st.nlink or 1,
                    "st_uid": st.uid, "st_gid": st.gid,
                    "st_atime": st.atime, "st_mtime": st.mtime, "st_ctime": st.ctime}

        def readdir(self, path, fh):
            yield "."
            yield ".."
            for e in view.listdir(path):
                yield e.name

        def read(self, path, size, offset, fh):
            return view.read(path, offset, size)

        def write(self, path, data, offset, fh):
            view.write(path, data) if offset == 0 else view.ops.write(view.real(path), data, offset)
            return len(data)

        def create(self, path, mode, fi=None):
            view.write(path, b"")
            return 0

        def truncate(self, path, length, fh=None):
            cur = view.read(path)
            view.write(path, cur[:length] + b"\0" * max(0, length - len(cur)))

        def mkdir(self, path, mode):
            view.mkdir(path)

        def rmdir(self, path):
            view.rmdir(path)

        def unlink(self, path):
            view.remove(path)

        def rename(self, old, new):
            view.rename(old, new)

        def chmod(self, path, mode):
            try:
                view.ops.chmod(view.real(path), mode)
            except Exception:
                pass

        def flush(self, path, fh):
            return 0

    def _run():
        FUSE(_VdiFuse(), mountpoint, foreground=True, nothreads=True,
             ro=readonly, allow_other=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
