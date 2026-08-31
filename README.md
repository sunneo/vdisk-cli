# vdi — Virtual Disk Image toolkit

Create / convert virtual disk images (**vmdk / vhdx / iso**) from a folder, and
do full read-write CRUD on the filesystems inside them
(**FAT16 / FAT32 / exFAT / ext2 / ext3 / ext4**) — plus a daemon/session mode and
an **MCP server** so Claude or other AIs can list, read, and modify files inside
an image.

Full design: [DESIGN.md](DESIGN.md).

## Why

`vdi` doesn't rely on the host OS's filesystem drivers. It attaches the image to
a Linux environment and uses the Linux kernel's own drivers, so every one of the
six filesystems is readable **and writable** on any OS, with the same commands.

## Engines

`--engine` picks the backend (all behind one interface):

| engine | what | status |
|--------|------|--------|
| `wsl`   | libguestfs / guestfish inside an existing WSL2 distro | working, end-to-end tested |
| `qemu`  | **self-contained**: bundled `qemu-system` + a ~18 MB Linux appliance — no WSL | working (M2); ship a `qemu-system` binary for Windows |
| `local` | maps a host directory as the filesystem — for tests / debugging | working |

`--engine auto` (default) picks the first usable one.

## Install

```bash
python -m pip install -e .
vdi doctor
```

Python 3.10+. Then set up one engine:

<details><summary><b>wsl engine</b> (Windows, uses an existing WSL2 distro)</summary>

```bash
sudo apt-get update
sudo apt-get install -y libguestfs-tools qemu-utils xorriso linux-image-generic
sudo chmod 0644 /boot/vmlinuz-*                 # supermin needs to read it
sudo usermod -aG kvm "$USER"                    # optional: KVM instead of slow TCG
printf '[boot]\ncommand = chmod 0666 /dev/kvm\n' | sudo tee /etc/wsl.conf
# then, from Windows:  wsl --terminate <distro>
```
</details>

<details><summary><b>qemu engine</b> (self-contained, no WSL)</summary>

```bash
# build the appliance once (needs a Linux/WSL box with busybox-static +
# linux-image-generic + e2fsprogs/dosfstools/exfatprogs/util-linux)
vdi appliance build          # or: bash appliance/build.sh

# put a qemu-system-x86_64 on PATH, in appliance/qemu/, or set $VDI_QEMU
vdi doctor
```
Release tarballs bundle the appliance; on Windows drop `qemu-system-x86_64.exe`
into `appliance\qemu\`.
</details>

`vdi doctor` checks every requirement and prints the exact fix for anything missing.

## Usage

```bash
# 1. build an image from a folder
vdi image create ./payload out.vmdk --fs ext4  --size 4G --label DATA
vdi image create ./payload out.vhdx --fs exfat --size 8G
vdi image create ./payload out.iso

# 2. convert
vdi image convert out.vmdk out.vhdx
vdi image info  out.vhdx
vdi image parts out.vhdx

# 3. CRUD on files inside  (TARGET = <image>[@<partition>][:<path>])
vdi fs ls    out.vmdk@1:/ -l
vdi fs read  out.vmdk:/etc/hostname
vdi fs write out.vmdk:/etc/hostname --content "myhost"
vdi fs cp-in  ./localdir out.vmdk:/opt/app -r
vdi fs cp-out out.vmdk:/opt/app ./localdir -r
vdi fs mkdir out.vmdk:/a/b/c -p
vdi fs grep  "TODO" out.vmdk:/src -i
vdi fs stat  out.vmdk:/x --json
vdi fs df    out.vmdk@1

# 4. serve it like a mount  (any of these frontends, together)
vdi serve out.vmdk --name disk1 \
    --ftp 127.0.0.1:2121 \
    --webdav 127.0.0.1:8080 \
    --mount /mnt/disk1 \          # FUSE (Linux/macOS; needs fusepy)
    --mcp 127.0.0.1:7333 --mcp-writable
vdi sessions
vdi session info disk1

# 5. another vdi (or you) reaches the running one
vdi fs ls   --session disk1 /opt
vdi fs write disk1:/opt/x --content ...        # or the NAME:/path shorthand
# a one-shot `vdi fs` for an image that already has a session reuses it (~0.5s)

# 6. let an AI in — MCP over stdio
vdi mcp out.vmdk@1                 # read-only
vdi mcp out.vmdk@1 --writable --root /srv

# or just point-and-click
vdi gui

# not sure if it's working? -v shows engine steps + timings, -vv traces every command
vdi -v fs ls out.vmdk@1:/
```

## GUI

`vdi gui` opens a tkinter file manager: open any image (pick engine / partition /
read-only) or attach to a running `vdi serve` session, then browse, view/edit
text files, import/export files and folders, make/rename/delete directories, and
run *Create image from folder* / *Convert* / *Serve* from the menus. All work
happens on a background thread so an engine boot never freezes the window.
(Linux needs the distro's `python3-tk`.)

Claude Desktop `claude_desktop_config.json`:

```jsonc
{ "mcpServers": {
    "vdi-disk": { "command": "vdi", "args": ["mcp", "C:\\images\\disk.vmdk@1"] } } }
```

MCP tools: `list_dir` `stat` `read_file` `read_file_text` `write_file` `mkdir`
`rmdir` `remove` `rename` `copy_in` `copy_out` `disk_info` `grep`.

## Protocol

Client ↔ daemon is JSON-RPC 2.0 over loopback TCP with a per-session token. At
connect the two negotiate a codec (`hello` frame): `json`, or **`toon`**
(Token-Oriented Object Notation — directory listings / grep hits become one
header + bare rows, ~half the bytes). File bytes ride a raw length-prefixed
trailer, never an encoding. See `src/vdi/wire.py`.

## Status

| area | state |
|------|-------|
| image create / convert / info / parts | ✅ wsl + qemu, tested |
| fs CRUD + grep (six filesystems, read+write) | ✅ tested |
| daemon + session discovery + one-shot reuse | ✅ tested |
| MCP frontend (stdio + TCP) | ✅ tested |
| FTP + WebDAV frontends (stdlib) | ✅ tested (`tests/test_frontends.py`) |
| FUSE frontend | ✅ Linux/macOS via fusepy |
| GUI (`vdi gui`, tkinter) | ✅ tested (`tests/test_gui.py`) |
| codec negotiation + TOON | ✅ tested |
| bundled-QEMU appliance (M2) | ✅ boots + builds + CRUD (`tests/e2e_qemu.py`) |
| packaging (PyInstaller + CI + release workflow) | ✅ `packaging/`, `.github/workflows/` |

`python -m pytest -q` — 37 tests (no WSL/QEMU needed).
`tests/e2e_wsl.py` / `tests/e2e_qemu.py` — live end-to-end against each real engine.

### Known limits

- Without a running `vdi serve`, each one-shot `fs` command boots the engine
  (~1–50 s depending on KVM vs TCG) — use `-v` to watch it, or start
  `vdi serve <image>` once and every one-shot after that reuses it (~0.5 s).
- Windows-native `qemu` engine needs a `qemu-system-x86_64.exe` you supply.
- exFAT/FAT have no POSIX permissions — `chmod`/`chown` return `ENOTSUP`.
