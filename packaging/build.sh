#!/usr/bin/env bash
# Build a single-file `vdi` binary + a release tarball.
#   packaging/build.sh            # current platform
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

python -m pip install -q -e ".[dev]"

# appliance (Linux/WSL only; skipped elsewhere -- ship it from a Linux runner)
if [ -f /boot/vmlinuz-* ] 2>/dev/null && command -v busybox >/dev/null; then
    bash appliance/build.sh || echo "appliance build skipped"
fi

pyinstaller --clean -y packaging/vdi.spec
ver="$(python -c 'import sys;sys.path.insert(0,"src");import vdi;print(vdi.__version__)')"
plat="$(python -c 'import platform;print(platform.system().lower()+"-"+platform.machine().lower())')"
out="dist/vdi-$ver-$plat"
mkdir -p "$out"
cp -r dist/vdi* "$out"/ 2>/dev/null || true
cp README.md DESIGN.md "$out"/
( cd dist && tar czf "vdi-$ver-$plat.tar.gz" "$(basename "$out")" )
echo "built dist/vdi-$ver-$plat.tar.gz"
