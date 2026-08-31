"""Image-level operations: create, convert, info, parts.

Format conversion and blank-image creation use ``qemu-img``. ISO building and
mkfs/partitioning happen inside the engine's Linux environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from vdi.errors import EngineError, VdiError

_EXT_TO_FMT = {
    ".vmdk": "vmdk",
    ".vhdx": "vhdx",
    ".vhd": "vpc",
    ".qcow2": "qcow2",
    ".img": "raw",
    ".raw": "raw",
}

_PASSTHROUGH = {"create", "convert", "info", "check", "resize", "snapshot",
                "commit", "rebase", "-p", "-c", "-f", "-O", "-o", "--output=json"}

_MKFS = {
    "fat16": ["mkfs.vfat", "-F", "16"],
    "fat32": ["mkfs.vfat", "-F", "32"],
    "vfat": ["mkfs.vfat", "-F", "32"],
    "exfat": ["mkfs.exfat"],
    "ext2": ["mkfs.ext2", "-F"],
    "ext3": ["mkfs.ext3", "-F"],
    "ext4": ["mkfs.ext4", "-F"],
}


def fmt_from_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext not in _EXT_TO_FMT:
        raise VdiError(f"cannot infer image format from {path!r}; use --format")
    return _EXT_TO_FMT[ext]


@dataclass
class Partition:
    device: str
    fs_type: str
    label: str
    uuid: str
    size_bytes: int


@dataclass
class ImageInfo:
    path: str
    format: str
    virtual_size: int
    actual_size: int
    partitions: list[Partition]


class QemuImg:
    """Thin wrapper over ``qemu-img``, on the host or via an engine."""

    def __init__(self, engine=None):
        self.engine = engine
        self._host = shutil.which("qemu-img")

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        if self._host:
            proc = subprocess.run([self._host, *args], capture_output=True)
        elif self.engine is not None and self.engine.supports_native_qemu_img():
            from vdi.engine.wsl import _wsl  # only wsl provides this today
            gargs = [self._translate(a) for a in args]
            proc = _wsl(["qemu-img", *gargs], distro=getattr(self.engine, "distro", None), check=False)
        else:
            raise EngineError("qemu-img not found on host and no engine provides it")
        if proc.returncode != 0:
            raise EngineError("qemu-img " + " ".join(args) + "\n" + proc.stderr.decode("replace"))
        return proc

    def _translate(self, arg: str) -> str:
        # qemu-img subcommands / flags / sizes pass through; anything that names
        # or could name a file on the host gets mapped into the engine's view.
        if not self.engine or arg.startswith("-") or arg in _PASSTHROUGH:
            return arg
        if "=" in arg and not os.path.exists(arg):   # -o key=val payloads
            return arg
        looks_pathish = (
            os.path.sep in arg or (len(arg) > 1 and arg[1] == ":")
            or arg.startswith(".") or os.path.splitext(arg)[1] != ""
        )
        if looks_pathish:
            try:
                return self.engine.wsl_path(arg)
            except Exception:
                return arg
        return arg

    def create_blank(self, path: str, fmt: str, size: str) -> None:
        self._run(["create", "-f", fmt, path, size])

    def convert(self, src: str, dst: str, *, src_fmt: str | None = None,
                dst_fmt: str | None = None, compress: bool = False,
                subformat: str | None = None, preallocation: str | None = None) -> None:
        args = ["convert", "-p"]
        if src_fmt:
            args += ["-f", src_fmt]
        args += ["-O", dst_fmt or fmt_from_path(dst)]
        opts = []
        if subformat:
            opts.append(f"subformat={subformat}")
        if preallocation:
            opts.append(f"preallocation={preallocation}")
        if opts:
            args += ["-o", ",".join(opts)]
        if compress:
            args.append("-c")
        args += [src, dst]
        self._run(args)

    def check(self, path: str) -> None:
        self._run(["check", path])

    def info(self, path: str) -> dict:
        proc = self._run(["info", "--output=json", path])
        return json.loads(proc.stdout.decode())
