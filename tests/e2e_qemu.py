"""Live check of the bundled-QEMU appliance engine. Run where qemu-system-x86_64
+ qemu-img exist and appliance/build/ is populated:

    python tests/e2e_qemu.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vdi.engine.qemu import QemuApplianceEngine


def main():
    eng = QemuApplianceEngine()
    info = eng.probe()
    print("probe:", info.available, info.detail)
    assert info.available, "qemu engine not available"

    work = ROOT / "_e2eq"
    os.system(f"rm -rf {work}")
    (work / "docs").mkdir(parents=True)
    (work / "readme.txt").write_bytes(b"hello from qemu appliance\n")
    (work / "docs" / "n.txt").write_bytes(b"one\ntwo\nthree\n")
    img = work / "disk.vmdk"

    print("# build ext4 vmdk ...")
    t0 = time.time()
    eng.build_from_folder(str(work), str(img), fmt="vmdk", fs="ext4",
                          size="96M", label="QAPP", part_table="gpt")
    print(f"  built in {time.time()-t0:.1f}s, {img.stat().st_size} bytes")

    print("# open + CRUD ...")
    with eng.open_image(str(img)) as d:
        assert d.fs_type() == "ext4", d.fs_type()
        names = {e.name for e in d.ls("/")}
        assert {"readme.txt", "docs"} <= names, names
        assert d.read("/readme.txt") == b"hello from qemu appliance\n"

        blob = bytes(range(256)) * 4       # 1 KiB, keeps the hex console path quick
        d.write("/sub/new.bin", blob)
        assert d.read("/sub/new.bin") == blob
        d.mkdir("/e", parents=True)
        d.rename("/sub/new.bin", "/e/moved.bin")
        assert d.stat("/e/moved.bin").size == len(blob)
        d.rm("/e/moved.bin")

        hits = d.grep("two", "/")
        assert hits and hits[0].path == "/docs/n.txt" and hits[0].line == 2, hits

        rec = sorted(e.name for e in d.ls("/", recursive=True))
        assert "/docs/n.txt" in rec, rec

    print("\nQEMU APPLIANCE E2E PASSED")


if __name__ == "__main__":
    main()
