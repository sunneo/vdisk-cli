"""GUI smoke test: build the window against the local engine without mainloop."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app():
    from vdi import log
    try:
        from vdi.gui import VdiGui
        a = VdiGui()
    except tk.TclError:
        pytest.skip("no display")
    a.withdraw()
    yield a
    log.remove_sink(a._log_sink)
    try:
        a.destroy()
    except tk.TclError:
        pass


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


def test_activity_log_receives_engine_steps(app):
    import time
    from vdi import log
    log.step("wsl: booting appliance VM")
    for _ in range(100):
        app.update()
        if "booting appliance VM" in app.act_txt.get("1.0", "end"):
            break
        time.sleep(0.02)
    assert "booting appliance VM" in app.act_txt.get("1.0", "end")
    assert app.act_visible
