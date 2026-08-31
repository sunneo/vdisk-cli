"""MCP server frontend -- let Claude / other AIs read & write files inside an image.

Two ways to run (see DESIGN.md 4.4):
    vdi mcp <target>                 one-shot, MCP over stdio
    vdi serve <image> --mcp <bind>   attach to a running session

This module needs the optional ``mcp`` package (``pip install vdi-converter[mcp]``).
The tool surface is defined here in a transport-agnostic way so it can also be
exposed without the SDK if needed.
"""
from __future__ import annotations

import base64
import contextlib
import json
import threading

from vdi.fsops import FilesystemOps

READ_TOOLS = {"list_dir", "stat", "read_file", "read_file_text", "disk_info", "grep"}
WRITE_TOOLS = {"write_file", "mkdir", "rmdir", "remove", "rename", "copy_in", "copy_out"}


def tool_specs(writable: bool) -> list[dict]:
    specs = [
        {"name": "list_dir", "description": "List a directory inside the image.",
         "input": {"path": "str", "recursive": "bool?"}},
        {"name": "stat", "description": "File attributes: type, size, mode, uid/gid, times, inode.",
         "input": {"path": "str"}},
        {"name": "read_file", "description": "Read bytes (base64) from a file.",
         "input": {"path": "str", "offset": "int?", "length": "int?"}},
        {"name": "read_file_text", "description": "Read a file as text, truncated to max_bytes.",
         "input": {"path": "str", "max_bytes": "int?"}},
        {"name": "disk_info", "description": "Partition table and per-partition fs type / usage.",
         "input": {}},
        {"name": "grep", "description": "Recursively search file contents inside the image.",
         "input": {"pattern": "str", "path": "str", "glob": "str?"}},
    ]
    if writable:
        specs += [
            {"name": "write_file", "description": "Create/overwrite/append a file.",
             "input": {"path": "str", "content": "str", "encoding": "str?", "append": "bool?"}},
            {"name": "mkdir", "description": "Create a directory.", "input": {"path": "str", "parents": "bool?"}},
            {"name": "rmdir", "description": "Remove a directory.", "input": {"path": "str", "recursive": "bool?"}},
            {"name": "remove", "description": "Remove a file.", "input": {"path": "str"}},
            {"name": "rename", "description": "Rename/move within the image.", "input": {"src": "str", "dst": "str"}},
            {"name": "copy_in", "description": "Copy a host path into the image.",
             "input": {"host_path": "str", "dst": "str"}},
            {"name": "copy_out", "description": "Copy an image path out to the host.",
             "input": {"src": "str", "host_path": "str"}},
        ]
    return specs


class McpTools:
    """Transport-agnostic implementation of the MCP tool surface."""

    def __init__(self, ops: FilesystemOps, *, writable: bool = False, root: str = "/",
                 lock: "threading.Lock | None" = None):
        self.ops = ops
        self.writable = writable
        self.root = root.rstrip("/") or ""
        self._lock = lock or contextlib.nullcontext()

    def _abs(self, path: str) -> str:
        from vdi.target import normalize_inner
        p = path if path.startswith("/") else "/" + path
        full = normalize_inner(self.root + "/" + p)
        root = self.root or ""
        if root and not (full == root or full.startswith(root + "/")):
            from vdi.errors import PermissionDenied
            raise PermissionDenied(f"path escapes --mcp-root: {path}")
        return full or "/"

    def _guard_write(self, name: str):
        if not self.writable:
            from vdi.errors import ReadOnly
            raise ReadOnly(f"{name}: MCP server is read-only (start with --mcp-writable)")

    def call(self, name: str, args: dict) -> dict:
        if name in WRITE_TOOLS:
            self._guard_write(name)
        handler = getattr(self, "t_" + name, None)
        if handler is None:
            from vdi.errors import VdiError
            raise VdiError(f"unknown tool: {name}")
        with self._lock:
            return handler(**args)

    # -- read tools ----------------------------------------------
    def t_list_dir(self, path, recursive=False):
        return {"entries": [e.dict() for e in self.ops.ls(self._abs(path), long=True, recursive=recursive)]}

    def t_stat(self, path):
        return self.ops.stat(self._abs(path)).dict()

    def t_read_file(self, path, offset=0, length=None):
        data = self.ops.read(self._abs(path), offset, length)
        return {"encoding": "base64", "data": base64.b64encode(data).decode(), "length": len(data)}

    def t_read_file_text(self, path, max_bytes=262144):
        data = self.ops.read(self._abs(path), 0, max_bytes + 1)
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", "replace")
        return {"text": text, "truncated": truncated}

    def t_disk_info(self):
        return self.ops.df().dict()

    def t_grep(self, pattern, path="/", glob=None, ignore_case=False):
        hits = self.ops.grep(pattern, self._abs(path), glob=glob, ignore_case=ignore_case)
        return {"matches": [h.dict() for h in hits]}

    # -- write tools ---------------------------------------------
    def t_write_file(self, path, content, encoding="utf-8", append=False):
        raw = base64.b64decode(content) if encoding == "base64" else content.encode()
        self.ops.write(self._abs(path), raw, append=append)
        return {"written": len(raw)}

    def t_mkdir(self, path, parents=False):
        self.ops.mkdir(self._abs(path), parents=parents)
        return {"ok": True}

    def t_rmdir(self, path, recursive=False):
        self.ops.rmdir(self._abs(path), recursive=recursive)
        return {"ok": True}

    def t_remove(self, path):
        self.ops.rm(self._abs(path))
        return {"ok": True}

    def t_rename(self, src, dst):
        self.ops.rename(self._abs(src), self._abs(dst))
        return {"ok": True}

    def t_copy_in(self, host_path, dst):
        self.ops.upload_tree(host_path, self._abs(dst))
        return {"ok": True}

    def t_copy_out(self, src, host_path):
        self.ops.download_tree(self._abs(src), host_path)
        return {"ok": True}


def run_stdio(tools: McpTools) -> int:
    """Serve MCP over stdio. Uses the official SDK when present, else a tiny
    line-delimited JSON-RPC shim implementing the subset MCP clients need."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception:
        return _run_stdio_shim(tools)

    server = FastMCP("vdi")
    for spec in tool_specs(tools.writable):
        name = spec["name"]

        def make(n):
            def _tool(**kwargs):
                return tools.call(n, kwargs)
            _tool.__name__ = n
            _tool.__doc__ = spec["description"]
            return _tool

        server.tool(name=name, description=spec["description"])(make(name))
    server.run(transport="stdio")
    return 0


def handle_mcp_line(tools: McpTools, line: str) -> str | None:
    """Process one line of the MCP JSON-RPC shim; return the reply line or None."""
    line = line.strip()
    if not line:
        return None
    req = json.loads(line)
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    try:
        if method in ("initialize",):
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "vdi", "version": "0.0.1"}}
        elif method in ("notifications/initialized", "initialized"):
            return None
        elif method == "tools/list":
            result = {"tools": [
                {"name": s["name"], "description": s["description"],
                 "inputSchema": {"type": "object"}} for s in tool_specs(tools.writable)]}
        elif method == "tools/call":
            out = tools.call(params["name"], params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out)}]}
        else:
            return json.dumps({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32601, "message": method}})
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
    except Exception as e:
        return json.dumps({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32000, "message": repr(e)}})


def _run_stdio_shim(tools: McpTools) -> int:
    import sys
    for line in sys.stdin:
        reply = handle_mcp_line(tools, line)
        if reply is not None:
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()
    return 0


def serve_tcp_shim(tools: McpTools, host: str, port: int):
    """Serve the MCP shim over a TCP socket (tool-to-tool / advanced use).

    Returns a started ``socketserver.ThreadingTCPServer``; caller runs/stops it.
    """
    import socketserver

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for raw in self.rfile:
                reply = handle_mcp_line(tools, raw.decode("utf-8", "replace"))
                if reply is not None:
                    self.wfile.write(reply.encode() + b"\n")

    srv = socketserver.ThreadingTCPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
