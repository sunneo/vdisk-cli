"""Bundled-QEMU appliance engine -- the "獨立在系統之外" path (no WSL).

``vdi`` ships (or downloads) ``qemu-system-x86_64`` + a ~15 MB Linux appliance
(``appliance/build/{vmlinuz,initramfs.gz}``). We boot it with the target image
attached as virtio-blk, and drive a BusyBox shell over a virtio console exactly
like the WSL engine drives guestfish: one persistent session, sentinel-framed
commands, an error never tears it down.

Accel: KVM (Linux) / WHPX (Windows) / HVF (macOS), falling back to TCG.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from vdi.engine.base import Engine, EngineInfo, OpenImage
from vdi.errors import EngineError, NotFound, ReadOnly, Unsupported
from vdi.fsops import DfInfo, DirEntry, GrepHit, StatInfo, NO_POSIX_PERMS

def _appliance_dir() -> Path:
    # env override, then PyInstaller bundle, then the source tree, then a
    # user data dir (where `vdi appliance build` / a downloaded release lands)
    if os.environ.get("VDI_APPLIANCE"):
        return Path(os.environ["VDI_APPLIANCE"])
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "appliance" / "build"
    src = Path(__file__).resolve().parents[3] / "appliance" / "build"
    if (src / "vmlinuz").exists():
        return src
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "vdi" / "appliance"
    else:
        base = Path.home() / ".local" / "share" / "vdi" / "appliance"
    return base if (base / "vmlinuz").exists() else src


APPLIANCE = _appliance_dir()
_NO_PERMS = NO_POSIX_PERMS


def _find_qemu() -> str | None:
    for name in ("VDI_QEMU",):
        if os.environ.get(name):
            return os.environ[name]
    local = APPLIANCE.parent / "qemu" / ("qemu-system-x86_64" + (".exe" if os.name == "nt" else ""))
    if local.exists():
        return str(local)
    return shutil.which("qemu-system-x86_64")


def _accel_args() -> list[str]:
    if sys.platform == "linux" and os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return ["-accel", "kvm", "-cpu", "host"]
    if sys.platform == "win32":
        return ["-accel", "whpx,kernel-irqchip=off", "-accel", "tcg", "-cpu", "max"]
    if sys.platform == "darwin":
        return ["-accel", "hvf", "-accel", "tcg", "-cpu", "max"]
    return ["-accel", "tcg", "-cpu", "max"]


class QemuApplianceEngine(Engine):
    name = "qemu"

    def probe(self, *, deep: bool = False) -> EngineInfo:
        qemu = _find_qemu()
        kern = (APPLIANCE / "vmlinuz").exists()
        initrd = (APPLIANCE / "initramfs.gz").exists()
        ok = bool(qemu) and kern and initrd
        q = "yes" if qemu else "missing (PATH / appliance/qemu/ / $VDI_QEMU)"
        a = "yes" if (kern and initrd) else "missing (run appliance/build.sh)"
        detail = f"qemu-system-x86_64={q}; appliance={a}"
        if ok and deep:
            try:
                with self.open_image(os.devnull, None, readonly=True):
                    pass
            except Exception as e:
                return EngineInfo(self.name, False, detail + f"; boot FAILED: {e}")
            detail += "; boot OK"
        return EngineInfo(self.name, ok, detail)

    def supports_native_qemu_img(self) -> bool:
        return bool(shutil.which("qemu-img"))

    def wsl_path(self, p: str) -> str:      # host paths are already native here
        return os.path.abspath(p)

    def open_image(self, image, partition=None, *, readonly=False) -> "QemuOpenImage":
        qemu = _find_qemu()
        if not qemu:
            raise EngineError("qemu-system-x86_64 not found; run 'vdi doctor'")
        if not (APPLIANCE / "vmlinuz").exists():
            raise EngineError(f"appliance not built: run {APPLIANCE.parent / 'build.sh'}")
        return QemuOpenImage(qemu, os.path.abspath(image), partition, readonly=readonly)

    # image building reuses the same appliance
    _FS = {"fat16": "vfat", "fat32": "vfat", "vfat": "vfat", "exfat": "exfat",
           "ext2": "ext2", "ext3": "ext3", "ext4": "ext4"}

    def build_from_folder(self, folder, out, *, fmt, fs, size, label="", part_table="gpt"):
        if fs not in self._FS:
            raise EngineError(f"unsupported --fs {fs!r}")
        qi = shutil.which("qemu-img")
        if not qi:
            raise EngineError("qemu-img needed for image creation")
        out = os.path.abspath(out)
        subprocess.run([qi, "create", "-f", fmt, out, size], check=True, capture_output=True)

        import tempfile
        tarpath = tempfile.mkstemp(prefix="vdi-payload-", suffix=".tar")[1]
        subprocess.run(["tar", "-C", os.path.abspath(folder), "-cf", tarpath, "."], check=True)
        try:
            img = QemuOpenImage(_find_qemu(), out, None, readonly=False,
                                _raw_disk=True, aux_file=tarpath)
            try:
                # partition table + a single partition; if the tiny appliance
                # kernel won't re-enumerate vda1, fall back to a whole-disk fs.
                tbl = "gpt" if part_table != "mbr" else "dos"
                img.sh(f"printf 'label: {tbl}\\n,,L\\n' | sfdisk --no-reread --no-tell-kernel /dev/vda",
                       check=False)
                img.sh("partx -a /dev/vda 2>/dev/null; blockdev --rereadpt /dev/vda 2>/dev/null; "
                       "mdev -s; i=0; while [ ! -e /dev/vda1 ] && [ $i -lt 20 ]; do usleep 100000; "
                       "mdev -s; i=$((i+1)); done", check=False)
                _, has_part = img.sh("test -e /dev/vda1", check=False)
                dev = "/dev/vda1" if has_part == 0 else "/dev/vda"
                if dev == "/dev/vda":
                    img.sh("sfdisk --delete /dev/vda 2>/dev/null; dd if=/dev/zero of=/dev/vda bs=1M "
                           "count=1 2>/dev/null; sync", check=False)

                gfs = self._FS[fs]
                mk = {"vfat": ["mkfs.vfat"] + (["-n", label[:11]] if label else []),
                      "exfat": ["mkfs.exfat"] + (["-L", label] if label else []),
                      "ext2": ["mkfs.ext2", "-F", "-q"], "ext3": ["mkfs.ext3", "-F", "-q"],
                      "ext4": ["mkfs.ext4", "-F", "-q"]}[gfs]
                if label and gfs.startswith("ext"):
                    mk += ["-L", label]
                img.sh(" ".join(shlex.quote(x) for x in mk) + f" {dev}", check=False)
                _, rc = img.sh(f"blkid {dev} | grep -q TYPE", check=False)
                if rc != 0:
                    raise EngineError(f"mkfs {gfs} on {dev} produced no recognisable filesystem")
                img.sh(f"mkdir -p /mnt && mount {dev} /mnt")
                img.sh("tar -C /mnt -xf /dev/vdb && sync && umount /mnt")
            finally:
                img.close()
        finally:
            os.unlink(tarpath)

    def build_iso(self, folder, out, *, volid="", boot=None):
        for tool in ("xorriso", "genisoimage", "mkisofs"):
            exe = shutil.which(tool)
            if exe:
                args = ([exe, "-as", "mkisofs"] if tool == "xorriso" else [exe])
                args += ["-R", "-J"]
                if volid:
                    args += ["-V", volid]
                args += ["-o", os.path.abspath(out), self.wsl_path(folder)]
                subprocess.run(args, check=True, capture_output=True)
                return
        raise EngineError("need xorriso or genisoimage on PATH to build ISOs")


def _read_chunk(pipe, timeout):
    q: "queue.Queue[bytes]" = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: q.put(pipe.recv(65536)), daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


class _Console:
    """Sentinel-framed BusyBox shell over the appliance's serial console.

    The console tty echoes input and we can't reliably turn that off from inside,
    so: (1) the end marker is held in a shell variable ``$M`` set once at boot --
    commands only ever contain ``$M``, never the literal value, so the marker
    appears solely in real output; (2) each command is sent as ONE line, so
    exactly one echoed line has to be stripped from the front of the result.
    """

    MARK = "vZvZmark"          # $M$M expands to this + this

    def __init__(self, sock: socket.socket):
        self.s = sock
        self._buf = bytearray()
        self._lock = threading.RLock()
        self._marker = (self.MARK + self.MARK).encode()

    def _read_until(self, needle: bytes, timeout: float) -> bytes:
        while needle not in bytes(self._buf):
            chunk = _read_chunk(self.s, timeout)
            if not chunk:
                raise EngineError("appliance console timeout; got:\n"
                                  + bytes(self._buf).decode("utf-8", "replace")[-800:])
            self._buf += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        idx = bytes(self._buf).index(needle) + len(needle)
        out, self._buf = bytes(self._buf[:idx]), bytearray(self._buf[idx:])
        return out

    def _read_rest_of_line(self, timeout: float) -> bytes:
        while b"\n" not in bytes(self._buf):
            chunk = _read_chunk(self.s, timeout)
            if not chunk:
                break
            self._buf += chunk
        line, _, rest = bytes(self._buf).partition(b"\n")
        self._buf = bytearray(rest)
        return line

    def handshake(self, timeout: float = 60) -> None:
        self._read_until(b"vdi-appliance ready", timeout)
        self.s.sendall((f'M={self.MARK}; export PATH=/bin:/sbin:/usr/bin:/usr/sbin; '
                        f'printf "\\n%s%s READY\\n" "$M" "$M"\n').encode())
        self._read_until(self._marker + b" READY", timeout)
        self._read_rest_of_line(timeout)

    def sh(self, cmd: str, *, check: bool = True, timeout: float = 300) -> tuple[str, int]:
        one = cmd.replace("\n", " ; ")
        mk = self._marker
        with self._lock:
            # start + end markers held in $M so the echoed line never contains
            # the marker value -- terminal echo (which we can't disable, and which
            # line-wraps long commands) is thus harmless.
            self.s.sendall(
                (f'printf "%s%s@S@\\n" "$M" "$M" ; {{ {one} ; }} ; __r=$? ; '
                 f'printf "\\n%s%s@E@%s\\n" "$M" "$M" "$__r"\n').encode())
            self._read_until(mk + b"@S@\n", timeout)                # skip echo + real start
            head = self._read_until(mk + b"@E@", timeout)           # <out>\n<MARK>@E@
            rc_bytes = self._read_rest_of_line(timeout)             # <rc>
        body = head[: -(len(mk) + 3)]
        digits = rc_bytes.strip()
        rc = int(digits) if digits.isdigit() else 1
        text = body.decode("utf-8", "replace").strip("\n")
        if os.environ.get("VDI_DEBUG") == "1":
            sys.stderr.write(f"[qemu] {cmd[:70]!r} rc={rc} out={text[:120]!r}\n")
        # drop the echoed command line (first line) if the shell echoed it
        if check and rc != 0:
            raise EngineError(f"appliance: {cmd.splitlines()[0][:80]} -> rc={rc}\n{text[-400:]}")
        return text, rc

    def read_bin(self, path: str, timeout: float = 600) -> bytes:
        out, rc = self.sh(f"xxd -p {shlex.quote(path)}", check=False, timeout=timeout)
        if rc != 0:
            raise NotFound(path)
        return bytes.fromhex("".join(out.split()))

    def write_bin(self, path: str, data: bytes, timeout: float = 600) -> None:
        h = data.hex()
        self.sh(": > /tmp/.hx", timeout=timeout)
        for i in range(0, len(h), 6000):        # keep shell lines sane
            self.sh(f"printf %s {h[i:i+6000]} >> /tmp/.hx", timeout=timeout)
        self.sh(f"xxd -r -p /tmp/.hx > {shlex.quote(path)} && rm -f /tmp/.hx", timeout=timeout)


class QemuOpenImage(OpenImage):
    fs = "unknown"

    def __init__(self, qemu, image, partition, *, readonly=False, _raw_disk=False,
                 aux_file=None):
        self.image = image
        self.readonly = readonly
        self.aux_file = aux_file          # attached as /dev/vdb (raw)
        self._proc = None
        self._con: _Console | None = None
        self._boot(qemu)
        if not _raw_disk:
            self._mount(partition)

    # -- boot -------------------------------------------------
    def _boot(self, qemu):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(120)
        port = srv.getsockname()[1]
        fmt = _fmt_of(self.image) or "raw"
        boot_timeout = 120 if "-accel tcg" in " ".join(_accel_args()) else 40
        args = [qemu, "-M", "q35", "-m", "512M", "-no-reboot", "-nographic",
                "-nodefaults", "-no-user-config",
                *_accel_args(),
                "-kernel", str(APPLIANCE / "vmlinuz"),
                "-initrd", str(APPLIANCE / "initramfs.gz"),
                "-append", "console=ttyS0 reboot=t panic=-1 quiet",
                "-chardev", f"socket,id=con,host=127.0.0.1,port={port},nodelay=on",
                "-serial", "chardev:con",
                "-drive", f"file={self.image},if=none,id=d0,format={fmt}"
                          + (",readonly=on" if self.readonly else ""),
                "-device", "virtio-blk-pci,drive=d0"]
        if self.aux_file:
            args += ["-drive", f"file={self.aux_file},if=none,id=d1,format=raw",
                     "-device", "virtio-blk-pci,drive=d1"]
        self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            self.close()
            err = (self._proc.stderr.read() or b"").decode("utf-8", "replace")[-600:]
            raise EngineError(f"appliance did not connect back:\n{err}")
        finally:
            srv.close()
        conn.settimeout(None)
        self._con = _Console(conn)
        self._con.handshake(boot_timeout)

    def sh(self, *a, **k):
        return self._con.sh(*a, **k)

    def put_bytes(self, path, tb64_source: bytes):
        self._con.write_bin(path, tb64_source)

    # -- mount -----------------------------------------------
    def _mount(self, partition):
        self._con.sh("mdev -s 2>/dev/null; true", check=False)
        devs, _ = self._con.sh("ls /dev/vda* 2>/dev/null || true", check=False)
        cand = [d for d in devs.split() if d.startswith("/dev/vda")]
        parts = [d for d in cand if d != "/dev/vda"] or cand
        if partition:
            if partition.isdigit():
                parts = [f"/dev/vda{partition}"]
            elif partition.startswith("/dev/"):
                parts = [partition.replace("sda", "vda")]
        opt = "-o ro " if self.readonly else ""
        last = ""
        for d in parts:
            _, rc = self._con.sh(f"mount {opt}{d} /mnt", check=False)
            if rc == 0:
                t, _ = self._con.sh("cat /proc/mounts | awk '$2==\"/mnt\"{print $3}'", check=False)
                self.fs = t.strip() or "unknown"
                self._dev = d
                return
            last = d
        raise EngineError(f"could not mount any of {parts} (last {last})")

    # -- FilesystemOps -------------------------------------
    def fs_type(self):
        return self.fs

    def open(self, *a, **k):
        pass

    def close(self):
        try:
            if self._con:
                self._con.sh("sync; umount /mnt 2>/dev/null; poweroff -f 2>/dev/null || true",
                             check=False, timeout=15)
        except Exception:
            pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.wait(timeout=8)
            except Exception:
                self._proc.kill()

    def _q(self, p):
        return shlex.quote("/mnt" + (p if p.startswith("/") else "/" + p))

    def df(self):
        out, _ = self._con.sh(f"df -k /mnt | tail -1", check=False)
        parts = out.split()
        if len(parts) >= 4:
            total, used, avail = (int(parts[1]) * 1024, int(parts[2]) * 1024, int(parts[3]) * 1024)
        else:
            total = used = avail = 0
        return DfInfo(self.fs, total, used, avail)

    def ls(self, path, *, long=False, recursive=False):
        if recursive:
            out, rc = self._con.sh(f"find {self._q(path)} -mindepth 1", check=False)
        else:
            out, rc = self._con.sh(f"ls -1A {self._q(path)}", check=False)
        if rc != 0:
            raise NotFound(path)
        res = []
        for n in out.splitlines():
            n = n.strip()
            if not n:
                continue
            full = n[len("/mnt"):] if n.startswith("/mnt") else _join(path, n)
            res.append(self._entry(full, display=(full if recursive else n)))
        return res

    def _entry(self, full, *, display=None):
        st = self.stat(full)
        return DirEntry(name=display if display is not None else full.rsplit("/", 1)[-1] or "/",
                        type=st.type, size=st.size, mtime=st.mtime, mode=st.mode)

    def stat(self, path) -> StatInfo:
        out, rc = self._con.sh(
            f"stat -c '%f %s %a %u %g %X %Y %Z %h %i %F' {self._q(path)}", check=False)
        if rc != 0:
            raise NotFound(path)
        f = out.split()
        kind = " ".join(f[10:])
        typ = ("dir" if "directory" in kind else "symlink" if "symbolic" in kind
               else "file" if "file" in kind else "other")
        return StatInfo(type=typ, size=int(f[1]), mode=f[2].rjust(4, "0"),
                        uid=int(f[3]), gid=int(f[4]), atime=int(f[5]), mtime=int(f[6]),
                        ctime=int(f[7]), nlink=int(f[8]), inode=int(f[9]))

    def read(self, path, offset=0, length=None) -> bytes:
        data = self._con.read_bin("/mnt" + (path if path.startswith("/") else "/" + path))
        if offset or length is not None:
            return data[offset: (offset + length) if length is not None else None]
        return data

    def tree_size(self, path, *, apparent=False) -> int:
        out, rc = self._con.sh(f"du -sk {self._q(path)}", check=False)
        if rc != 0:
            raise NotFound(path)
        return int(out.split()[0]) * 1024

    def _guard(self):
        if self.readonly:
            raise ReadOnly()

    def write(self, path, data: bytes, offset=0, *, append=False):
        self._guard()
        p = "/mnt" + (path if path.startswith("/") else "/" + path)
        self._con.sh(f"mkdir -p {shlex.quote(os.path.dirname(p))}", check=False)
        if append:
            cur = self.read(path) if self._exists(path) else b""
            data = cur + data
        elif offset:
            cur = self.read(path) if self._exists(path) else b""
            data = cur[:offset].ljust(offset, b"\0") + data
        self._con.write_bin(p, data)

    def _exists(self, path):
        _, rc = self._con.sh(f"test -e {self._q(path)}", check=False)
        return rc == 0

    def mkdir(self, path, *, parents=False):
        self._guard()
        self._con.sh(f"mkdir {'-p ' if parents else ''}{self._q(path)}")

    def rmdir(self, path, *, recursive=False):
        self._guard()
        self._con.sh(f"{'rm -rf' if recursive else 'rmdir'} {self._q(path)}")

    def rm(self, path):
        self._guard()
        self._con.sh(f"rm {self._q(path)}")

    def rename(self, src, dst):
        self._guard()
        self._con.sh(f"mv {self._q(src)} {self._q(dst)}")

    def chmod(self, path, mode: int):
        self._guard()
        if self.fs in _NO_PERMS:
            raise Unsupported(f"{self.fs} has no POSIX permissions")
        self._con.sh(f"chmod {oct(mode)[2:]} {self._q(path)}")

    def chown(self, path, uid, gid):
        self._guard()
        if self.fs in _NO_PERMS:
            raise Unsupported(f"{self.fs} has no POSIX ownership")
        self._con.sh(f"chown {uid}:{gid} {self._q(path)}")

    def upload_tree(self, local_path, dst):
        self._guard()
        tar = subprocess.run(["tar", "-C", local_path, "-cf", "-", "."],
                             capture_output=True, check=True).stdout
        self._con.sh(f"mkdir -p {self._q(dst)}")
        self._con.write_bin("/tmp/.up.tar", tar)
        self._con.sh(f"tar -C {self._q(dst)} -xf /tmp/.up.tar && rm -f /tmp/.up.tar", timeout=600)

    def download_tree(self, src, local_path):
        _, rc = self._con.sh(f"tar -C {self._q(src)} -cf /tmp/.dn.tar .", check=False, timeout=600)
        if rc != 0:
            raise NotFound(src)
        blob = self._con.read_bin("/tmp/.dn.tar")
        self._con.sh("rm -f /tmp/.dn.tar", check=False)
        os.makedirs(local_path, exist_ok=True)
        p = subprocess.Popen(["tar", "-C", local_path, "-xf", "-"], stdin=subprocess.PIPE)
        p.communicate(blob)

    def grep(self, pattern, path, *, glob=None, ignore_case=False, max_results=1000):
        flags = "-rnI" + ("i" if ignore_case else "")
        inc = f" --include={shlex.quote(glob)}" if glob else ""
        out, _ = self._con.sh(
            f"grep {flags}{inc} -e {shlex.quote(pattern)} {self._q(path)} 2>/dev/null "
            f"| head -n {int(max_results)} || true", check=False)
        hits = []
        for line in out.splitlines():
            rest = line[len("/mnt"):] if line.startswith("/mnt") else line
            p, _, r = rest.partition(":")
            n, _, txt = r.partition(":")
            if n.isdigit():
                hits.append(GrepHit(path=p, line=int(n), text=txt[:500]))
        return hits


def _fmt_of(image):
    low = image.lower()
    for ext, f in ((".vmdk", "vmdk"), (".vhdx", "vhdx"), (".vhd", "vpc"),
                   (".qcow2", "qcow2"), (".raw", "raw"), (".img", "raw"), (".iso", "raw")):
        if low.endswith(ext):
            return f
    return None


def _join(base, name):
    if name.startswith("/"):
        return name
    return (base.rstrip("/") + "/" + name) if base != "/" else "/" + name
