"""Typed errors shared by CLI, RPC and engine layers.

The ``code`` values double as JSON-RPC error codes (see DESIGN.md section 5.3).
"""
from __future__ import annotations


class VdiError(Exception):
    code = -32000
    message = "vdi error"

    def __init__(self, message: str | None = None, *, data=None):
        super().__init__(message or self.message)
        self.data = data

    def to_rpc(self) -> dict:
        err = {"code": self.code, "message": str(self)}
        if self.data is not None:
            err["data"] = self.data
        return err


class NotFound(VdiError):
    code = -32001
    message = "path does not exist"


class ReadOnly(VdiError):
    code = -32002
    message = "filesystem is mounted read-only"


class NotADirectory(VdiError):
    code = -32003
    message = "not a directory"


class DirectoryNotEmpty(VdiError):
    code = -32004
    message = "directory not empty"


class NoSpace(VdiError):
    code = -32005
    message = "no space left on device"


class PermissionDenied(VdiError):
    code = -32006
    message = "permission denied"


class Unsupported(VdiError):
    code = -32007
    message = "operation not supported by this filesystem"


class EngineError(VdiError):
    code = -32010
    message = "engine failure"


class SessionError(VdiError):
    code = -32020
    message = "session error"


_BY_CODE = {
    cls.code: cls
    for cls in (
        NotFound, ReadOnly, NotADirectory, DirectoryNotEmpty, NoSpace,
        PermissionDenied, Unsupported, EngineError, SessionError,
    )
}


def from_rpc(err: dict) -> VdiError:
    cls = _BY_CODE.get(err.get("code"), VdiError)
    exc = cls(err.get("message"))
    exc.data = err.get("data")
    return exc
