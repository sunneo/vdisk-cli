"""Engine backends.

An *engine* is what actually attaches a virtual disk to a Linux environment and
runs filesystem operations against it with the kernel's own drivers.

Backends:
    wsl   - run libguestfs/guestfish inside an existing WSL2 distro   (works today)
    qemu  - bundled QEMU + micro Linux appliance, no WSL needed       (distribution target)
    local - map a host directory as the filesystem                    (testing / debug only)
"""
from __future__ import annotations

from vdi.engine.base import Engine, EngineInfo
from vdi.errors import EngineError

# order matters: 'auto' tries these in turn. 'local' is opt-in only.
_BACKENDS = ("wsl", "qemu")
_ALL = ("wsl", "qemu", "local")


def available_engines() -> list[EngineInfo]:
    infos: list[EngineInfo] = []
    for name in _ALL:
        try:
            infos.append(_load(name).probe())
        except Exception as exc:  # pragma: no cover - defensive
            infos.append(EngineInfo(name=name, available=False, detail=str(exc)))
    return infos


def get_engine(name: str = "auto") -> Engine:
    from vdi import log
    if name == "auto":
        for cand in _BACKENDS:
            eng = _load(cand)
            info = eng.probe()
            log.trace(f"engine {cand}: {'available' if info.available else 'no'} - {info.detail}")
            if info.available:
                log.step(f"engine: {cand}")
                return eng
        raise EngineError(
            "no usable engine found. Run 'vdi doctor'. "
            "The 'wsl' engine needs a WSL2 distro with libguestfs-tools installed."
        )
    return _load(name)


def _load(name: str) -> Engine:
    from vdi import log
    log.trace(f"engine: loading {name}")
    if name == "wsl":
        from vdi.engine.wsl import WslEngine
        return WslEngine()
    if name == "qemu":
        from vdi.engine.qemu import QemuApplianceEngine
        return QemuApplianceEngine()
    if name == "local":
        from vdi.engine.local import LocalDirEngine
        return LocalDirEngine()
    raise EngineError(f"unknown engine: {name!r} (choose from {_ALL} or 'auto')")
