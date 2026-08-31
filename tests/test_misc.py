import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from vdi import log
from vdi.image import fmt_from_path, _EXT_TO_FMT
from vdi.errors import VdiError


def test_log_levels(capsys, monkeypatch):
    monkeypatch.setattr(log, "_level", 0)
    log.step("quiet"); log.trace("quiet")
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(log, "_level", 1)
    log.step("hi"); log.trace("not shown")
    err = capsys.readouterr().err
    assert "hi" in err and "not shown" not in err

    monkeypatch.setattr(log, "_level", 2)
    with log.waiting("job"):
        pass
    err = capsys.readouterr().err
    assert "job ..." in err and "job: done" in err


def test_set_level_only_raises(monkeypatch):
    monkeypatch.setattr(log, "_level", 0)
    log.set_level(2)
    assert log.level() == 2
    log.set_level(1)          # never lowers
    assert log.level() == 2


@pytest.mark.parametrize("name,fmt", [
    ("x.vmdk", "vmdk"), ("y.VHDX", "vhdx"), ("z.qcow2", "qcow2"), ("a.raw", "raw"),
])
def test_fmt_from_ext(name, fmt):
    assert fmt_from_path(name, probe=False) == fmt


def test_fmt_from_path_unknown_raises():
    with pytest.raises(VdiError) as ei:
        fmt_from_path("some/weird/name-no-ext", probe=False)
    assert "--format" in str(ei.value)
