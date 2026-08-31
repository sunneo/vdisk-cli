"""Host-side frontends that expose a mounted image over a familiar protocol.

Each takes a :class:`~vdi.fsops.FilesystemOps` (local one-shot handle or a
:class:`~vdi.remote.RemoteOps` talking to a daemon) plus a lock, and serves it:

    ftp      -> minimal RFC 959 server (stdlib only)
    webdav   -> WebDAV class-1/2 over http.server (stdlib only); Explorer / rclone
    fuse     -> real mount point on Linux (needs 'fusepy'); no-op elsewhere

They are started by ``vdi serve --ftp/--webdav/--mount`` and share the daemon's
single-writer lock.
"""
from __future__ import annotations

from vdi.frontend.ftp import FtpFrontend
from vdi.frontend.webdav import WebdavFrontend

__all__ = ["FtpFrontend", "WebdavFrontend", "start_fuse"]


def start_fuse(ops, mountpoint: str, *, readonly: bool = False, lock=None):
    from vdi.frontend.fuse import start_fuse as _s
    return _s(ops, mountpoint, readonly=readonly, lock=lock)
