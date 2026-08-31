"""Bundled-QEMU appliance engine (distribution target).

This is the "獨立在系統之外" path: ``vdi`` ships its own QEMU + a micro Linux
appliance (kernel + initramfs + vdi-agent). No WSL, no admin, no host FS drivers.

Not implemented yet -- see appliance/ and DESIGN.md sections 2 & 6. The class
exists so `--engine qemu` and `vdi doctor` report it honestly.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from vdi.engine.base import Engine, EngineInfo
from vdi.errors import EngineError

APPLIANCE_DIR = Path(__file__).resolve().parents[3] / "appliance" / "build"


class QemuApplianceEngine(Engine):
    name = "qemu"

    def probe(self) -> EngineInfo:
        qemu = shutil.which("qemu-system-x86_64")
        kernel = (APPLIANCE_DIR / "vmlinuz").exists()
        initramfs = (APPLIANCE_DIR / "initramfs.cpio.gz").exists()
        ok = bool(qemu) and kernel and initramfs
        detail = (
            f"qemu-system-x86_64={'yes' if qemu else 'missing'}; "
            f"appliance kernel={'yes' if kernel else 'missing'}; "
            f"initramfs={'yes' if initramfs else 'missing'}"
        )
        if not ok:
            detail += "  (build with: appliance/build.sh)"
        return EngineInfo(self.name, ok, detail)

    def open_image(self, image, partition=None, *, readonly=False):
        raise EngineError(
            "qemu appliance engine not implemented yet; use --engine wsl. "
            "Roadmap milestone M2 (see DESIGN.md)."
        )
