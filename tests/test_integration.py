"""End-to-end tests over the 'local' engine (no WSL/QEMU needed).

Covers: FilesystemOps semantics, JSON-RPC daemon + RemoteOps, session registry
discovery, and the MCP tool surface incl. the read-only guard and --mcp-root jail.
"""
import base64
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from vdi.engine.local import LocalDirEngine
from vdi.errors import NotFound, ReadOnly, PermissionDenied
from vdi.mcp_server import McpTools
from vdi.rpc import RpcServer, RpcClient, new_token
from vdi.remote import RemoteOps
from vdi.service import FsService
from vdi import registry


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "img"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "hostname").write_bytes(b"alpha\n")
    (root / "var" / "log").mkdir(parents=True)
    (root / "var" / "log" / "a.log").write_bytes(b"hello\nworld\n")
    return root


def test_local_ops_crud(tree):
    img = LocalDirEngine().open_image(str(tree))
    assert img.fs_type() == "hostdir"
    names = {e.name for e in img.ls("/etc")}
    assert names == {"hostname"}
    assert img.read("/etc/hostname") == b"alpha\n"

    img.mkdir("/opt/app", parents=True)
    img.write("/opt/app/config.txt", b"k=v\n")
    assert img.read("/opt/app/config.txt") == b"k=v\n"
    img.write("/opt/app/config.txt", b"more\n", append=True)
    assert img.read("/opt/app/config.txt") == b"k=v\nmore\n"

    img.rename("/opt/app/config.txt", "/opt/app/renamed.txt")
    assert img.stat("/opt/app/renamed.txt").type == "file"
    with pytest.raises(NotFound):
        img.stat("/opt/app/config.txt")

    img.rm("/opt/app/renamed.txt")
    img.rmdir("/opt/app", recursive=True)
    with pytest.raises(NotFound):
        img.ls("/opt/app")

    assert img.tree_size("/var") == len("hello\nworld\n")


def test_readonly_guard(tree):
    img = LocalDirEngine().open_image(str(tree), readonly=True)
    assert img.read("/etc/hostname") == b"alpha\n"
    with pytest.raises(ReadOnly):
        img.write("/etc/hostname", b"nope")


def test_path_escape(tree):
    img = LocalDirEngine().open_image(str(tree))
    with pytest.raises(PermissionDenied):
        img.read("/../../../etc/passwd")


def test_rpc_roundtrip_and_registry(tree, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))

    img = LocalDirEngine().open_image(str(tree))
    svc = FsService(img)
    token = new_token()
    server = RpcServer(svc.dispatch, token)
    server.start_thread()
    try:
        sess = registry.Session(
            name="t1", pid=__import__("os").getpid(), image=str(tree),
            image_format="dir", partition=None, fs_type="hostdir",
            readonly=False, engine="local",
            rpc=registry.RpcEndpoint(addr=server.addr, port=server.port, token=token),
        )
        registry.create(sess)

        found = registry.resolve("t1")
        assert found.rpc.port == server.port

        ops = RemoteOps(RpcClient(found.rpc.addr, found.rpc.port, found.rpc.token))
        assert ops.fs_type() == "hostdir"
        assert ops.read("/etc/hostname") == b"alpha\n"
        ops.mkdir("/new", parents=True)
        ops.write("/new/x", b"12345")
        assert ops.stat("/new/x").size == 5
        assert {e.name for e in ops.ls("/")} >= {"etc", "var", "new"}

        # raw binary channel (no base64): 2 MiB with NUL/newline bytes
        blob = bytes(range(256)) * 8192
        ops.write("/new/blob.bin", blob)
        assert ops.read("/new/blob.bin") == blob
        assert ops.tree_size("/new") == len(blob) + 5
    finally:
        server.shutdown()
        registry.remove("t1")


def test_mcp_tools_readonly_and_root(tree):
    img = LocalDirEngine().open_image(str(tree))

    ro = McpTools(img, writable=False, root="/var")
    listing = ro.call("list_dir", {"path": "/"})
    assert {e["name"] for e in listing["entries"]} == {"log"}  # jailed to /var

    txt = ro.call("read_file_text", {"path": "/log/a.log"})
    assert txt["text"] == "hello\nworld\n" and txt["truncated"] is False

    with pytest.raises(ReadOnly):
        ro.call("write_file", {"path": "/log/b.log", "content": "x"})

    with pytest.raises(PermissionDenied):
        ro.call("read_file", {"path": "/../etc/hostname"})

    rw = McpTools(img, writable=True, root="/")
    rw.call("write_file", {"path": "/etc/motd", "content": "aGk=", "encoding": "base64"})
    assert img.read("/etc/motd") == b"hi"
    hits = rw.call("grep", {"pattern": "world", "path": "/var"})
    assert hits["matches"] and hits["matches"][0]["line"] == 2
    assert hits["matches"][0]["path"] == "/var/log/a.log"


def test_grep_engine(tree):
    img = LocalDirEngine().open_image(str(tree))
    hits = img.grep("world", "/")
    assert [(h.path, h.line) for h in hits] == [("/var/log/a.log", 2)]
    assert img.grep("WORLD", "/", ignore_case=True)
    assert img.grep("world", "/", glob="*.md") == []
    assert img.grep("nomatch", "/") == []
