"""TARGET syntax parsing.

A *target* names an image, an optional partition, and an optional path inside it:

    <image>[@<partition>][:<in-image-path>]

Examples::

    disk.vmdk
    disk.vmdk:/etc/fstab
    disk.vmdk@1:/etc
    disk.vmdk@/dev/sda2:/boot
    C:\\images\\disk.vhdx:/Windows/System32
    /data/disk.qcow2@DATA:/

Session targets are written differently and parsed by :func:`parse_session_target`::

    build01:/opt/app          (with --session, or "name:/path")

In-image paths are always POSIX-absolute (they start with ``/``). That is what lets
us disambiguate the drive-letter colon on Windows (``C:\\`` is not a path marker
because it is not followed by ``/`` ... but ``C:/`` would be, so we special-case a
leading single drive letter).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
# Session names are simple identifiers: letters, digits, '_' and '-'. No dots,
# so "disk.vmdk:/etc" is unambiguously an image target, not "session:path".
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SESSION_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+):(?P<path>/.*)$")


@dataclass(frozen=True)
class Target:
    image: str                     # host path to the image file
    partition: str | None = None   # "1", "/dev/sda1", or a label; None = auto
    path: str = "/"                # POSIX-absolute path inside the filesystem

    @property
    def is_iso(self) -> bool:
        return self.image.lower().endswith(".iso")

    def with_path(self, path: str) -> "Target":
        return Target(self.image, self.partition, normalize_inner(path))


@dataclass(frozen=True)
class SessionTarget:
    session: str
    path: str = "/"


def _find_path_marker(s: str) -> int:
    """Return the index of the ``:`` that begins the in-image path, or -1.

    The in-image path marker is the first ``:`` that is immediately followed by
    ``/``, ignoring a leading Windows drive letter (``C:/`` / ``C:\\``).
    """
    start = 0
    if _DRIVE_PREFIX.match(s):
        start = 2  # skip "C:"
    for i in range(start, len(s) - 1):
        if s[i] == ":" and s[i + 1] == "/":
            return i
    return -1


def normalize_inner(path: str) -> str:
    """Normalize a path *inside* the image to a clean POSIX-absolute form.

    Rejects nothing here (escape checks live in the daemon VFS); this only
    collapses ``.``/``//`` and rewrites backslashes so callers can be sloppy.
    """
    if not path:
        return "/"
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts)


def parse_target(s: str) -> Target:
    """Parse a ``<image>[@<partition>][:<path>]`` string."""
    if not s:
        raise ValueError("empty target")

    marker = _find_path_marker(s)
    if marker >= 0:
        left, inner = s[:marker], s[marker + 1:]
    else:
        left, inner = s, "/"

    image, partition = left, None
    # Split a trailing "@partition". Guard against an "@" that is part of the
    # host path by only splitting on the last "@" that has no path separator
    # after it (partitions never contain "/" except the "/dev/..." form).
    at = left.rfind("@")
    if at != -1:
        cand_image, cand_part = left[:at], left[at + 1:]
        if cand_part and (not _looks_like_bare_at(cand_part) or cand_part.startswith("/dev/")):
            image, partition = cand_image, cand_part

    return Target(image=image, partition=partition, path=normalize_inner(inner))


def _looks_like_bare_at(part: str) -> bool:
    # A real partition spec: digits ("1"), "/dev/sdaN", or a label token.
    if part.startswith("/dev/"):
        return False
    if part.isdigit():
        return False
    # labels: letters/digits/_-. and no whitespace or slashes
    return not re.fullmatch(r"[A-Za-z0-9_.-]+", part or "")


def parse_session_target(s: str) -> SessionTarget:
    """Parse ``name:/path`` (used with ``--session`` or the bare session form)."""
    m = _SESSION_RE.match(s)
    if not m:
        raise ValueError(f"not a session target: {s!r} (expected name:/path)")
    return SessionTarget(session=m.group("name"), path=normalize_inner(m.group("path")))


def looks_like_session_target(s: str) -> bool:
    return bool(_SESSION_RE.match(s)) and not _WIN_ABS.match(s)
