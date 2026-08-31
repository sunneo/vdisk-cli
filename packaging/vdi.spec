# PyInstaller spec: single-file `vdi` executable.
#   pip install pyinstaller
#   pyinstaller packaging/vdi.spec
#
# The appliance (appliance/build/*) is bundled if present so `--engine qemu`
# works from the frozen binary. qemu-system-x86_64 is NOT bundled -- it must be
# on PATH, in appliance/qemu/, or pointed to by $VDI_QEMU.
import os
from PyInstaller.utils.hooks import collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))
appliance = os.path.join(root, "appliance", "build")

datas = []
if os.path.isdir(appliance):
    for f in os.listdir(appliance):
        datas.append((os.path.join(appliance, f), "appliance/build"))
datas.append((os.path.join(root, "appliance", "init"), "appliance"))
datas.append((os.path.join(root, "appliance", "build.sh"), "appliance"))

a = Analysis(
    [os.path.join(root, "src", "vdi", "__main__.py")],
    pathex=[os.path.join(root, "src")],
    datas=datas,
    hiddenimports=collect_submodules("vdi"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="vdi",
    console=True,
    upx=False,
    strip=False,
)
