"""Filesystem operation contract + data shapes.

Both the one-shot path and the daemon path speak this interface. The actual
implementation lives behind an :class:`~vdi.engine.base.Engine` (WSL or bundled
QEMU appliance); this module only defines the vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Protocol, runtime_checkable


@dataclass
class DirEntry:
    name: str
    type: str            # "file" | "dir" | "symlink" | "other"
    size: int
    mtime: int
    mode: str = "0000"
    target: str | None = None   # symlink target, if type == "symlink"

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class StatInfo:
    type: str
    size: int
    mode: str
    uid: int
    gid: int
    atime: int
    mtime: int
    ctime: int
    nlink: int
    inode: int

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class DfInfo:
    fs_type: str
    total_bytes: int
    used_bytes: int
    free_bytes: int

    def dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class FilesystemOps(Protocol):
    # lifecycle -----------------------------------------------------------
    def open(self, image: str, partition: str | None, *, readonly: bool) -> None: ...
    def close(self) -> None: ...
    def fs_type(self) -> str: ...
    def df(self) -> DfInfo: ...

    # read --------------------------------------------------------------
    def ls(self, path: str, *, long: bool = False, recursive: bool = False) -> list[DirEntry]: ...
    def stat(self, path: str) -> StatInfo: ...
    def read(self, path: str, offset: int = 0, length: int | None = None) -> bytes: ...
    def tree_size(self, path: str, *, apparent: bool = False) -> int: ...

    # write -----------------------------------------------------------
    def write(self, path: str, data: bytes, offset: int = 0, *, append: bool = False) -> None: ...
    def mkdir(self, path: str, *, parents: bool = False) -> None: ...
    def rmdir(self, path: str, *, recursive: bool = False) -> None: ...
    def rm(self, path: str) -> None: ...
    def rename(self, src: str, dst: str) -> None: ...
    def chmod(self, path: str, mode: int) -> None: ...
    def chown(self, path: str, uid: int, gid: int) -> None: ...

    # bulk ----------------------------------------------------------
    def upload_tree(self, local_path: str, dst: str) -> None: ...
    def download_tree(self, src: str, local_path: str) -> None: ...

    # search ------------------------------------------------------
    def grep(self, pattern: str, path: str, *, glob: str | None = None,
             ignore_case: bool = False, max_results: int = 1000) -> list["GrepHit"]: ...


@dataclass
class GrepHit:
    path: str
    line: int
    text: str

    def dict(self) -> dict:
        return asdict(self)


# Filesystems that carry no POSIX ownership/permission metadata.
NO_POSIX_PERMS = {"vfat", "fat", "fat12", "fat16", "fat32", "exfat", "ntfs", "ntfs3"}
