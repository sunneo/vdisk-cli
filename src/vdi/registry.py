"""Session registry: how a ``vdi`` process finds another one that has an image open.

Each ``vdi serve`` writes ``<registry>/<name>.json`` on start (O_EXCL, 0600) and
removes it on exit. Any ``vdi`` process lists the directory, liveness-checks each
entry, and reaps the dead ones.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def registry_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        d = Path(base) / "vdi" / "sessions"
    else:
        base = os.environ.get("XDG_RUNTIME_DIR")
        d = (Path(base) / "vdi" / "sessions") if base else Path.home() / ".vdi" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class RpcEndpoint:
    transport: str = "tcp"
    addr: str = "127.0.0.1"
    port: int = 0
    token: str = ""


@dataclass
class Session:
    name: str
    pid: int
    image: str
    image_format: str
    partition: str | None
    fs_type: str
    readonly: bool
    engine: str
    rpc: RpcEndpoint = field(default_factory=RpcEndpoint)
    mounts: dict = field(default_factory=dict)
    started_at: str = ""
    protocol_version: int = 1

    def path(self) -> Path:
        return registry_dir() / f"{self.name}.json"

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2)

    @classmethod
    def from_file(cls, p: Path) -> "Session":
        d = json.loads(p.read_text())
        d["rpc"] = RpcEndpoint(**d.get("rpc", {}))
        return cls(**d)


class RegistryConflict(RuntimeError):
    pass


def create(session: Session) -> Path:
    p = session.path()
    session.started_at = session.started_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if is_alive(Session.from_file(p)):
            raise RegistryConflict(f"session {session.name!r} already running")
        p.unlink(missing_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(session.to_json())
    return p


def remove(name: str) -> None:
    (registry_dir() / f"{name}.json").unlink(missing_ok=True)


def is_alive(s: Session) -> bool:
    if not _pid_alive(s.pid):
        return False
    if s.rpc.port:
        try:
            with socket.create_connection((s.rpc.addr, s.rpc.port), timeout=1.0):
                return True
        except OSError:
            return False
    return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_sessions(*, reap: bool = True) -> list[Session]:
    out: list[Session] = []
    for p in sorted(registry_dir().glob("*.json")):
        try:
            s = Session.from_file(p)
        except Exception:
            if reap:
                p.unlink(missing_ok=True)
            continue
        if is_alive(s):
            out.append(s)
        elif reap:
            p.unlink(missing_ok=True)
    return out


def resolve(name: str | None) -> Session:
    sessions = list_sessions()
    if name:
        for s in sessions:
            if s.name == name:
                return s
        raise KeyError(f"no live session named {name!r}")
    if not sessions:
        raise KeyError("no live sessions; start one with 'vdi serve <image>'")
    if len(sessions) > 1:
        names = ", ".join(s.name for s in sessions)
        raise KeyError(f"multiple sessions ({names}); pass --session NAME")
    return sessions[0]
