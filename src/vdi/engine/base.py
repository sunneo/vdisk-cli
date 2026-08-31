"""Engine interface + a session handle abstraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ContextManager

from vdi.fsops import FilesystemOps


@dataclass
class EngineInfo:
    name: str
    available: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


class Engine:
    """Base class for engine backends.

    Concrete backends implement :meth:`probe` and :meth:`open_image`.
    """

    name = "base"

    def probe(self) -> EngineInfo:  # pragma: no cover - abstract
        raise NotImplementedError

    def open_image(
        self,
        image: str,
        partition: str | None = None,
        *,
        readonly: bool = False,
    ) -> "OpenImage":
        """Attach *image*, mount *partition*, return an :class:`OpenImage`.

        The returned object is a context manager and implements
        :class:`~vdi.fsops.FilesystemOps`.
        """
        raise NotImplementedError

    # image-level helpers may be overridden for efficiency; defaults live in
    # vdi.image which shells out to qemu-img (native on host or via the engine).
    def supports_native_qemu_img(self) -> bool:
        return False


class OpenImage(ContextManager["OpenImage"], FilesystemOps):
    """A mounted image. Backend-specific; see wsl.py / qemu.py."""

    fs: str = "unknown"

    def __enter__(self) -> "OpenImage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def fs_type(self) -> str:
        return self.fs
