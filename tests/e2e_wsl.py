"""Live end-to-end check against the real WSL engine. Not run by default
(needs WSL + libguestfs). Usage:  python tests/e2e_wsl.py
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "MSYS_NO_PATHCONV": "1"}


def vdi(*args, **kw):
    return subprocess.run([sys.executable, "-m", "vdi", *args], env=ENV,
                          capture_output=True, text=True, **kw)


def main():
    work = ROOT / "_e2e"
    subprocess.run(["rm", "-rf", str(work)], shell=False)
    (work / "docs").mkdir(parents=True)
    (work / "readme.txt").write_bytes(b"hello from e2e\n")
    (work / "docs" / "a.txt").write_bytes(b"alpha\nbeta\n")
    img = work / "disk.vmdk"

    print("# image create ext4 ...")
    r = vdi("image", "create", str(work), str(img), "--fs", "ext4",
            "--size", "96M", "--label", "E2E", "--engine", "wsl")
    assert r.returncode == 0, r.stderr
    assert img.exists()

    print("# image convert -> vhdx ...")
    vhdx = work / "disk.vhdx"
    r = vdi("image", "convert", str(img), str(vhdx), "--engine", "wsl")
    assert r.returncode == 0, r.stderr

    print("# one-shot fs ls / read ...")
    r = vdi("fs", "ls", f"{img}@1:/", "-l", "--engine", "wsl")
    assert "readme.txt" in r.stdout, r.stdout + r.stderr
    r = vdi("fs", "read", f"{img}:/readme.txt", "--engine", "wsl")
    assert r.stdout == "hello from e2e\n", repr(r.stdout)

    print("# serve + session + MCP ...")
    d = subprocess.Popen([sys.executable, "-m", "vdi", "serve", str(img),
                          "--name", "e2e", "--engine", "wsl",
                          "--mcp", "127.0.0.1:7411", "--mcp-writable"],
                         env=ENV, stderr=subprocess.PIPE, text=True)
    try:
        ready = False
        for _ in range(60):
            line = d.stderr.readline()
            if line:
                print("  ", line.strip())
            if "ready" in line:
                ready = True
                break
        assert ready, "daemon did not become ready"

        r = vdi("sessions")
        assert "e2e" in r.stdout, r.stdout

        r = vdi("fs", "write", "e2e:/ai/added.txt", "--content", "via session")
        assert r.returncode == 0, r.stderr
        r = vdi("fs", "read", "e2e:/ai/added.txt")
        assert r.stdout == "via session", repr(r.stdout)

        # one-shot command reuses the running session (no fresh appliance boot)
        t0 = time.time()
        r = vdi("fs", "grep", "e2e from", str(img))
        assert "readme.txt" in r.stdout, r.stdout + r.stderr
        assert time.time() - t0 < 15, "one-shot did not reuse the warm session"

        # MCP over TCP
        with socket.create_connection(("127.0.0.1", 7411), timeout=10) as s:
            f = s.makefile("rwb", buffering=0)
            f.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                    b'{"name":"list_dir","arguments":{"path":"/"}}}\n')
            resp = json.loads(f.readline())
            names = json.loads(resp["result"]["content"][0]["text"])
            assert any(e["name"] == "readme.txt" for e in names["entries"]), names
            f.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
                    b'{"name":"write_file","arguments":{"path":"/ai/mcp.txt","content":"AI via MCP"}}}\n')
            json.loads(f.readline())
        r = vdi("fs", "read", "e2e:/ai/mcp.txt")
        assert r.stdout == "AI via MCP", repr(r.stdout)

        vdi("session", "stop", "e2e")
        time.sleep(3)
        assert "e2e" not in vdi("sessions").stdout
    finally:
        if d.poll() is None:
            d.terminate()

    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
