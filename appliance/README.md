# vdi appliance

The bundled-QEMU engine (`--engine qemu`) boots this micro Linux appliance,
attaches the target disk image, and runs `vdi-agent` inside it. This is the
"獨立在系統之外" path: no WSL, no admin, no host filesystem drivers.

## Contents (built into `build/`)

| file | what |
|------|------|
| `build/vmlinuz` | minimal x86_64 kernel with vfat/exfat/ext2-4/ntfs3/btrfs/xfs/isofs built in |
| `build/initramfs.cpio.gz` | busybox + e2fsprogs + exfatprogs + dosfstools + xorriso + `vdi-agent` |

## Build

```
./build.sh            # needs docker or podman; outputs build/vmlinuz + build/initramfs.cpio.gz
```

The build uses a Buildroot config (`buildroot.config`) plus an overlay that adds
`vdi-agent` and an init script that:

1. mounts `/proc` `/sys` `/dev`
2. loads virtio modules, opens the virtio-vsock control channel
3. execs `vdi-agent --vsock` which speaks the same JSON-RPC as the daemon
   (see `../DESIGN.md` section 5.3)

## Agent protocol

`vdi-agent` runs in the guest. The host `vdi` process:

- attaches the image as a virtio-blk device
- tells the agent `open {device, readonly}` -> agent mounts it at `/mnt`
- forwards `fs.*` calls; agent executes them with plain Linux syscalls
- on close, agent unmounts and the host tears down the VM

Status: not implemented. Milestone M0/M2 in `../DESIGN.md`.
