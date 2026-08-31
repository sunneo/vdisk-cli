"""GUI smoke test: build the window against the local engine without mainloop."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app():
    try:
        from vdi.gui import VdiGui
        a = VdiGui()
    except tk.TclError:
        pytest.skip("no display")
    a.withdraw()
    yield a
    a.destroy()


def test_open_and_browse(app, tmp_path):
    from vdi.gui import Conn
    from vdi.engine.local import LocalDirEngine

    root = tmp_path / "img"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"hi\n")
    (root / "top.txt").write_bytes(b"top\n")
    img = LocalDirEngine().open_image(str(root))

    import time

    def drain(pred, tries=100):
        for _ in range(tries):
            app.update()
            if pred():
                return
            time.sleep(0.02)

    app._connected(Conn(img, "test", False, img.close))
    drain(lambda: set(app.tree.get_children()) == {"sub", "top.txt"})
    assert set(app.tree.get_children()) == {"sub", "top.txt"}

    app.navigate("/sub")
    drain(lambda: "a.txt" in app.tree.get_children())
    assert "a.txt" in app.tree.get_children()


def test_helpers():
    from vdi.gui import _join, _human
    assert _join("/", "x") == "/x"
    assert _join("/a/b", "c") == "/a/b/c"
    assert _human(0) == "0B"
    assert _human(1536) == "1.5K"
    assert _human(5 * 1024 * 1024) == "5.0M"
