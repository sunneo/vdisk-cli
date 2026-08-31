"""FUSE frontend check (Linux/macOS, needs fusepy + libfuse). Run:
    python tests/e2e_fuse.py
Uses the local engine so it needs no WSL/QEMU.
"""
import os
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vdi.engine.local import LocalDirEngine
from vdi.frontend import start_fuse


def main():
    src = Path(tempfile.mkdtemp()) / "img"
    (src / "d").mkdir(parents=True)
    (src / "d" / "a.txt").write_bytes(b"alpha\n")
    (src / "top.txt").write_bytes(b"top\n")
    img = LocalDirEngine().open_image(str(src))

    mnt = tempfile.mkdtemp()
    start_fuse(img, mnt, readonly=False)
    time.sleep(1.5)
    try:
        assert sorted(os.listdir(mnt)) == ["d", "top.txt"], os.listdir(mnt)
        assert Path(mnt, "d", "a.txt").read_bytes() == b"alpha\n"

        Path(mnt, "d", "b.txt").write_bytes(b"written thru fuse\n")
        assert (src / "d" / "b.txt").read_bytes() == b"written thru fuse\n"

        os.mkdir(Path(mnt, "newdir"))
        assert (src / "newdir").is_dir()

        os.rename(Path(mnt, "d", "b.txt"), Path(mnt, "d", "c.txt"))
        assert (src / "d" / "c.txt").exists() and not (src / "d" / "b.txt").exists()

        os.remove(Path(mnt, "d", "c.txt"))
        assert not (src / "d" / "c.txt").exists()
        print("FUSE E2E PASSED")
    finally:
        os.system(f"fusermount -u {mnt} 2>/dev/null || fusermount3 -u {mnt} 2>/dev/null")


if __name__ == "__main__":
    main()
