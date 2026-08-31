"""FTP + WebDAV frontends over the local engine (stdlib clients)."""
import ftplib
import io
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from vdi.engine.local import LocalDirEngine
from vdi.frontend.common import FsView
from vdi.frontend.ftp import FtpFrontend
from vdi.frontend.webdav import WebdavFrontend


@pytest.fixture
def view(tmp_path):
    root = tmp_path / "img"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "a.txt").write_bytes(b"alpha\n")
    (root / "top.txt").write_bytes(b"top\n")
    img = LocalDirEngine().open_image(str(root))
    return FsView(img)


def test_ftp_roundtrip(view):
    fe = FtpFrontend(view, "127.0.0.1", 0, token="tok")
    fe.start()
    host, port = fe.host, fe.port
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=5)
        ftp.login("user", "tok")

        assert "top.txt" in ftp.nlst("/")
        ftp.cwd("/dir")
        assert ftp.nlst() == ["a.txt"] or "a.txt" in ftp.nlst()

        buf = io.BytesIO()
        ftp.retrbinary("RETR a.txt", buf.write)
        assert buf.getvalue() == b"alpha\n"

        ftp.storbinary("STOR b.txt", io.BytesIO(b"written\n"))
        assert view.read("/dir/b.txt") == b"written\n"
        assert ftp.size("b.txt") == 8

        ftp.mkd("/newdir")
        assert view.is_dir("/newdir")
        ftp.rename("/dir/b.txt", "/dir/c.txt")
        assert view.read("/dir/c.txt") == b"written\n"
        ftp.delete("/dir/c.txt")
        assert not view.exists("/dir/c.txt")
        ftp.quit()
    finally:
        fe.stop()


def test_ftp_bad_token(view):
    fe = FtpFrontend(view, "127.0.0.1", 0, token="tok")
    fe.start()
    try:
        ftp = ftplib.FTP()
        ftp.connect(fe.host, fe.port, timeout=5)
        with pytest.raises(ftplib.error_perm):
            ftp.login("user", "wrong")
    finally:
        fe.stop()


def _req(method, url, data=None, token="tok", **headers):
    r = urllib.request.Request(url, data=data, method=method)
    import base64
    r.add_header("Authorization", "Basic " + base64.b64encode(f"x:{token}".encode()).decode())
    for k, v in headers.items():
        r.add_header(k.replace("_", "-"), v)
    return urllib.request.urlopen(r, timeout=5)


def test_webdav_roundtrip(view):
    fe = WebdavFrontend(view, "127.0.0.1", 0, token="tok")
    fe.start()
    base = f"http://{fe.host}:{fe.port}"
    try:
        assert _req("GET", base + "/dir/a.txt").read() == b"alpha\n"

        _req("PUT", base + "/dir/new.txt", data=b"hello dav")
        assert view.read("/dir/new.txt") == b"hello dav"

        r = _req("PROPFIND", base + "/dir", Depth="1")
        body = r.read().decode()
        assert "a.txt" in body and "new.txt" in body and r.status == 207

        _req("MKCOL", base + "/coll")
        assert view.is_dir("/coll")

        _req("MOVE", base + "/dir/new.txt", Destination=base + "/dir/moved.txt")
        assert view.read("/dir/moved.txt") == b"hello dav"

        _req("DELETE", base + "/dir/moved.txt")
        assert not view.exists("/dir/moved.txt")
    finally:
        fe.stop()
