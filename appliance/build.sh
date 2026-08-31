#!/usr/bin/env bash
# Build the vdi micro appliance (kernel + initramfs) for the bundled-QEMU engine.
# Skeleton -- fills in during milestone M0. Requires docker or podman.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/build"
mkdir -p "$out"

echo "vdi appliance build -- NOT IMPLEMENTED YET"
echo
echo "Planned steps:"
echo "  1. docker build a Buildroot toolchain image from buildroot.config"
echo "  2. cross-compile cmd/vdi-agent (static) into the rootfs overlay"
echo "  3. produce $out/vmlinuz and $out/initramfs.cpio.gz"
echo
echo "Until then, use: vdi <cmd> --engine wsl"
exit 1
