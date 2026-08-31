"""Tiny stderr logger with a verbosity level.

    0  quiet (default)
    1  -v   high-level steps, with elapsed seconds  ("starting engine", "booting appliance", ...)
    2  -vv  everything above + per-command trace of what the engine sends

``VDI_DEBUG=1`` / ``VDI_VERBOSE=N`` in the environment set the level too, so
subprocesses (``vdi serve`` spawned by the GUI, tests) can be made chatty.
"""
from __future__ import annotations

import os
import sys
import time

_t0 = time.time()


def _env_level() -> int:
    if os.environ.get("VDI_DEBUG") == "1":
        return 2
    try:
        return max(0, min(2, int(os.environ.get("VDI_VERBOSE", "0"))))
    except ValueError:
        return 0


_level = _env_level()


def set_level(n: int) -> None:
    global _level
    _level = max(_level, int(n))
    os.environ["VDI_VERBOSE"] = str(_level)   # propagate to child processes


def level() -> int:
    return _level


def step(msg: str) -> None:
    if _level >= 1:
        sys.stderr.write(f"[vdi +{time.time() - _t0:5.1f}s] {msg}\n")
        sys.stderr.flush()


def trace(msg: str) -> None:
    if _level >= 2:
        sys.stderr.write(f"[vdi]  {msg}\n")
        sys.stderr.flush()


class waiting:
    """Context manager: with -v, print `msg …` then `  done (Ns)` / `  failed`."""

    def __init__(self, msg: str):
        self.msg = msg

    def __enter__(self):
        self._t = time.time()
        step(self.msg + " ...")
        return self

    def __exit__(self, exc_type, *_):
        if _level >= 1:
            dt = time.time() - self._t
            tail = "done" if exc_type is None else "FAILED"
            sys.stderr.write(f"[vdi +{time.time() - _t0:5.1f}s]   {self.msg}: {tail} ({dt:.1f}s)\n")
            sys.stderr.flush()
        return False
