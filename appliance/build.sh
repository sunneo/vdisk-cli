#!/usr/bin/env bash
# Build the vdi micro appliance (kernel + initramfs) for the bundled-QEMU engine.
#
# Runs on a Linux/WSL box with: busybox-static, a distro kernel + modules
# (linux-image-generic), e2fsprogs, dosfstools, exfatprogs, util-linux, cpio, gzip.
# No docker, no buildroot.
#
#   ./build.sh [KERNEL_VERSION]
#
# Output:  build/vmlinuz  build/initramfs.gz  build/appliance.json
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/build"
kver="${1:-$(ls -1 /boot/vmlinuz-* 2>/dev/null | sed 's#.*/vmlinuz-##' | sort -V | tail -1)}"
[ -n "$kver" ] || { echo "no /boot/vmlinuz-*; install linux-image-generic" >&2; exit 1; }
moddir="/lib/modules/$kver"
bb="$(command -v busybox)"
[ -n "$bb" ] || { echo "install busybox-static" >&2; exit 1; }
[ -r "/boot/vmlinuz-$kver" ] || { echo "sudo chmod 0644 /boot/vmlinuz-*" >&2; exit 1; }

echo "[build] kernel $kver"
rm -rf "$out" && mkdir -p "$out"
root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
mkdir -p "$root"/{bin,sbin,lib,lib64,proc,sys,dev,mnt,tmp,run,root,usr/bin,usr/sbin,lib/x86_64-linux-gnu}

cp "$bb" "$root/bin/busybox"
cp "$here/init" "$root/init"
chmod +x "$root/init" "$root/bin/busybox"

# -- real userspace tools busybox lacks, with their shared-lib closure --------
copybin() {
    local b; b="$(command -v "$1" 2>/dev/null || true)"
    [ -n "$b" ] || { echo "  ! missing $1" >&2; return 0; }
    local d="$root${b}"; mkdir -p "$(dirname "$d")"; cp -L "$b" "$d"
    ldd "$b" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | while read -r lib; do
        [ -e "$root$lib" ] && continue
        mkdir -p "$root$(dirname "$lib")"; cp -L "$lib" "$root$lib"
    done
}
for t in mke2fs mkfs.ext2 mkfs.ext3 mkfs.ext4 e2label tune2fs \
         mkfs.vfat mkfs.fat fatlabel \
         mkfs.exfat exfatlabel \
         sfdisk blkid partx findmnt; do
    copybin "$t"
done
# ld loader
for ld in /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-x86-64.so.2; do
    [ -e "$ld" ] && { mkdir -p "$root$(dirname "$ld")"; cp -L "$ld" "$root$ld"; }
done

# -- kernel modules (fs + crc/nls deps); depmod resolves the graph at boot -----
mkdir -p "$root/lib/modules/$kver"
for sub in fs/fat fs/exfat fs/ext2 fs/ext4 fs/isofs fs/udf fs/ntfs3 fs/btrfs fs/xfs fs/nls \
           fs/jbd2 fs/mbcache.ko lib/crc16.ko lib/libcrc32c.ko \
           crypto/crc32c_generic.ko arch/x86/crypto/crc32c-intel.ko \
           drivers/virtio drivers/block/virtio_blk.ko drivers/char/virtio_console.ko \
           drivers/net/virtio_net.ko ; do
    src="$moddir/kernel/$sub"; [ -e "$src" ] || continue
    dst="$root/lib/modules/$kver/kernel/$sub"; mkdir -p "$(dirname "$dst")"; cp -r "$src" "$dst"
done
for f in modules.order modules.builtin modules.builtin.modinfo; do
    [ -e "$moddir/$f" ] && cp "$moddir/$f" "$root/lib/modules/$kver/" || true
done
depmod -b "$root" "$kver" 2>/dev/null || true

( cd "$root" && find . | "$bb" cpio -o -H newc --quiet ) | gzip -9 > "$out/initramfs.gz"
cp "/boot/vmlinuz-$kver" "$out/vmlinuz"
printf '{ "kernel_version": "%s", "console": "ttyS0", "built": "%s" }\n' \
    "$kver" "$(date -u +%FT%TZ)" > "$out/appliance.json"

echo "[build] done:"; ls -la "$out"
