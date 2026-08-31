import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from vdi.target import (
    parse_target, parse_session_target, normalize_inner,
    looks_like_session_target,
)


@pytest.mark.parametrize("s,image,part,path", [
    ("disk.vmdk", "disk.vmdk", None, "/"),
    ("disk.vmdk:/etc/fstab", "disk.vmdk", None, "/etc/fstab"),
    ("disk.vmdk@1:/etc", "disk.vmdk", "1", "/etc"),
    ("disk.vmdk@/dev/sda2:/boot", "disk.vmdk", "/dev/sda2", "/boot"),
    ("disk.qcow2@DATA:/", "disk.qcow2", "DATA", "/"),
    (r"C:\images\disk.vhdx:/Windows/System32", r"C:\images\disk.vhdx", None, "/Windows/System32"),
    (r"C:\images\disk.vhdx", r"C:\images\disk.vhdx", None, "/"),
    (r"C:\x\disk.vmdk@2:/e", r"C:\x\disk.vmdk", "2", "/e"),
    ("/data/disk.img:/a/b", "/data/disk.img", None, "/a/b"),
])
def test_parse_target(s, image, part, path):
    t = parse_target(s)
    assert (t.image, t.partition, t.path) == (image, part, path)


def test_normalize_inner():
    assert normalize_inner("") == "/"
    assert normalize_inner("a/b") == "/a/b"
    assert normalize_inner("/a//b/./c") == "/a/b/c"
    assert normalize_inner("/a/b/../c") == "/a/c"
    assert normalize_inner("\\a\\b") == "/a/b"


def test_session_target():
    st = parse_session_target("build01:/opt/app")
    assert st.session == "build01" and st.path == "/opt/app"
    with pytest.raises(ValueError):
        parse_session_target("build01")  # no path


def test_looks_like_session_target():
    assert looks_like_session_target("build01:/opt")
    assert not looks_like_session_target(r"C:\images\disk.vhdx")
    assert not looks_like_session_target("disk.vmdk:/etc")  # has a dot -> ambiguous, treat as image
