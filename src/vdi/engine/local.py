"""Local-directory engine: treat a host directory as the mounted filesystem.

Not a virtual-disk engine -- it never touches vmdk/vhdx. It exists so the whole
stack above the engine (CLI -> service -> RPC -> MCP -> session discovery) can be
exercised end-to-end without WSL or QEMU, and as an ``--engine local`` debugging
aid for pointing the daemon / MCP frontend at a plain folder.

``vdi fs ls ./some/dir`` and ``vdi mcp ./some/dir --engine local`` work directly.
"""
from __future__ import annotations

import os
import shutil
import stat as _stat
import tarfile
from pathlib import Path

from vdi.engine.base import Engine, EngineInfo, OpenImage
from vdi.errors import (
    DirectoryNotEmpty, NotADirectory, NotFound, PermissionDenied, ReadOnly,
)
from vdi.fsops import DfInfo, DirEntry, StatInfo


def _is_regex(p: str) -> bool:
    return any(c in p for c in ".^$*+?()[]{}|\\")


class LocalDirEngine(Engine):
    name = "local"

    def probe(self) -> EngineInfo:
        return EngineInfo(self.name, True, "maps a host directory as a filesystem (testing/debug)")

    def open_image(self, image, partition=None, *, readonly=False) -> "LocalDirImage":
        root = Path(image).resolve()
        if not root.exists():
            root.mkdir(parents=True)
        if not root.is_dir():
            raise NotADirectory(f"{root} is not a directory")
        return LocalDirImage(root, readonly=readonly)


class LocalDirImage(OpenImage):
    fs = "hostdir"

    def __init__(self, root: Path, *, readonly: bool):
        self.root = root
        self.readonly = readonly

    # -- path mapping --------------------------------------------
    def _real(self, path: str) -> Path:
        rel = path.lstrip("/")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise PermissionDenied(f"path escapes root: {path}")
        return p

    def _guard(self):
        if self.readonly:
            raise ReadOnly()

    # -- lifecycle ----------------------------------------------
    def open(self, *a, **k):
        pass

    def close(self):
        pass

    def df(self) -> DfInfo:
        u = shutil.disk_usage(self.root)
        return DfInfo("hostdir", u.total, u.used, u.free)

    # -- read -------------------------------------------------
    def _entry(self, p: Path) -> DirEntry:
        st = p.lstat()
        if _stat.S_ISDIR(st.st_mode):
            t = "dir"
        elif _stat.S_ISLNK(st.st_mode):
            t = "symlink"
        elif _stat.S_ISREG(st.st_mode):
            t = "file"
        else:
            t = "other"
        return DirEntry(name=p.name, type=t, size=st.st_size, mtime=int(st.st_mtime),
                        mode=oct(st.st_mode & 0o7777)[2:].rjust(4, "0"),
                        target=os.readlink(p) if t == "symlink" else None)

    def ls(self, path, *, long=False, recursive=False):
        p = self._real(path)
        if not p.exists():
            raise NotFound(path)
        if not p.is_dir():
            return [self._entry(p)]
        out = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(p):
                for n in sorted(dirnames + filenames):
                    fp = Path(dirpath) / n
                    e = self._entry(fp)
                    e.name = "/" + str(fp.relative_to(self.root)).replace(os.sep, "/")
                    out.append(e)
        else:
            for child in sorted(p.iterdir()):
                out.append(self._entry(child))
        return out

    def stat(self, path) -> StatInfo:
        p = self._real(path)
        if not p.exists() and not p.is_symlink():
            raise NotFound(path)
        st = p.lstat()
        if _stat.S_ISDIR(st.st_mode):
            t = "dir"
        elif _stat.S_ISLNK(st.st_mode):
            t = "symlink"
        elif _stat.S_ISREG(st.st_mode):
            t = "file"
        else:
            t = "other"
        return StatInfo(type=t, size=st.st_size, mode=oct(st.st_mode & 0o7777)[2:].rjust(4, "0"),
                        uid=getattr(st, "st_uid", 0), gid=getattr(st, "st_gid", 0),
                        atime=int(st.st_atime), mtime=int(st.st_mtime), ctime=int(st.st_ctime),
                        nlink=st.st_nlink, inode=st.st_ino)

    def read(self, path, offset=0, length=None) -> bytes:
        p = self._real(path)
        if not p.is_file():
            raise NotFound(path)
        with p.open("rb") as fh:
            if offset:
                fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def tree_size(self, path, *, apparent=False) -> int:
        p = self._real(path)
        if not p.exists():
            raise NotFound(path)
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dp, _, fns in os.walk(p):
            for n in fns:
                try:
                    total += (Path(dp) / n).stat().st_size
                except OSError:
                    pass
        return total

    # -- write ------------------------------------------------
    def write(self, path, data: bytes, offset=0, *, append=False):
        self._guard()
        p = self._real(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with p.open("ab") as fh:
                fh.write(data)
        elif offset:
            with p.open("r+b" if p.exists() else "wb") as fh:
                fh.seek(offset)
                fh.write(data)
        else:
            p.write_bytes(data)

    def mkdir(self, path, *, parents=False):
        self._guard()
        p = self._real(path)
        p.mkdir(parents=parents, exist_ok=parents)

    def rmdir(self, path, *, recursive=False):
        self._guard()
        p = self._real(path)
        if not p.is_dir():
            raise NotADirectory(path)
        if recursive:
            shutil.rmtree(p)
        else:
            try:
                p.rmdir()
            except OSError:
                raise DirectoryNotEmpty(path)

    def rm(self, path):
        self._guard()
        p = self._real(path)
        if not p.exists():
            raise NotFound(path)
        p.unlink()

    def rename(self, src, dst):
        self._guard()
        s = self._real(src)
        if not s.exists():
            raise NotFound(src)
        d = self._real(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def chmod(self, path, mode: int):
        self._guard()
        self._real(path).chmod(mode)

    def chown(self, path, uid, gid):
        self._guard()
        if not hasattr(os, "chown"):
            from vdi.errors import Unsupported
            raise Unsupported("chown not available on this host")
        os.chown(self._real(path), uid, gid)

    # -- bulk -------------------------------------------------
    def upload_tree(self, local_path, dst):
        self._guard()
        src = Path(local_path)
        d = self._real(dst)
        if src.is_dir():
            shutil.copytree(src, d / src.name if d.is_dir() else d, dirs_exist_ok=True)
        else:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, d)

    def grep(self, pattern, path, *, glob=None, ignore_case=False, max_results=1000):
        import fnmatch
        import re as _re
        from vdi.fsops import GrepHit
        rx = _re.compile(_re.escape(pattern) if not _is_regex(pattern) else pattern,
                         _re.IGNORECASE if ignore_case else 0)
        root = self._real(path)
        hits: list[GrepHit] = []
        walk = [root] if root.is_file() else (Path(dp) / n
                for dp, _, fns in os.walk(root) for n in fns)
        for fp in walk:
            if glob and not fnmatch.fnmatch(fp.name, glob):
                continue
            try:
                text = fp.read_bytes()
            except OSError:
                continue
            if b"\0" in text[:8192]:
                continue
            for i, ln in enumerate(text.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(ln):
                    rel = "/" + str(fp.relative_to(self.root)).replace(os.sep, "/")
                    hits.append(GrepHit(path=rel, line=i, text=ln[:500]))
                    if len(hits) >= max_results:
                        return hits
        return hits

    def download_tree(self, src, local_path):
        s = self._real(src)
        if not s.exists():
            raise NotFound(src)
        out = Path(local_path)
        if s.is_dir():
            shutil.copytree(s, out / s.name if out.is_dir() else out, dirs_exist_ok=True)
        else:
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, out)
