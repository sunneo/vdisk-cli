"""WSL engine backend: drive libguestfs/guestfish inside an existing WSL2 distro.

Works on a Windows box with WSL2 + ``libguestfs-tools`` + a distro kernel package
(``linux-image-generic``; supermin needs a readable ``/boot/vmlinuz-*``).

WSL2 reaps a daemonised process once the ``wsl.exe`` that spawned it exits -- even
if the distro stays up. So the ``guestfish --listen`` daemon must remain a child
of a long-lived process: we keep one persistent ``bash`` (:class:`GuestfishSession`)
and run every command through that one shell, framed with a per-call sentinel.
Binary payloads travel as base64 (heredoc in / ``base64 -w0`` out).
"""
from __future__ import annotations

import base64
import os
import queue
import secrets
import shlex
import subprocess
import threading

from vdi import log


def _dbg(*a):
    log.trace("wsl " + " ".join(str(x) for x in a))

from vdi.engine.base import Engine, EngineInfo, OpenImage
from vdi.errors import EngineError, NotFound, ReadOnly, Unsupported
from vdi.fsops import DfInfo, DirEntry, StatInfo, NO_POSIX_PERMS

_SWAP_OR_UNKNOWN = {"swap", "unknown", ""}


def _rand() -> str:
    return secrets.token_hex(5)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _wsl(args: list[str], *, distro: str | None, input_bytes: bytes | None = None,
         check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = ["wsl.exe"] + (["-d", distro] if distro else []) + ["--", *args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise EngineError(f"wsl {shlex.join(args)} (rc={proc.returncode})\n"
                          + proc.stderr.decode("utf-8", "replace").strip())
    return proc


def _read_chunk(pipe, timeout: float) -> bytes | None:
    q: "queue.Queue[bytes]" = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: q.put(pipe.readline()), daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


class GfError(EngineError):
    pass


class GuestfishSession:
    """One persistent ``bash`` owning a ``guestfish --listen`` daemon."""

    def __init__(self, distro: str | None):
        self.distro = distro
        self.pid: str | None = None
        self._p: subprocess.Popen | None = None
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------
    def start(self, *, prewarm: bool = True) -> None:
        if prewarm:
            with log.waiting("wsl: preparing libguestfs appliance (first run can take 30-60s)"):
                _wsl(["sh", "-c", "guestfish -a /dev/null run : quit"],
                     distro=self.distro, check=False, timeout=600)
        cmd = ["wsl.exe"] + (["-d", self.distro] if self.distro else []) + \
              ["--", "bash", "--noprofile", "--norc"]
        self._p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, bufsize=0)
        self._feed('exec 2>&1\n'
                   'eval "$(guestfish --listen)"\n'
                   'printf "VDIREADY:%s\\n" "$GUESTFISH_PID"\n')
        log.step("wsl: starting guestfish listener")
        line = self._read_until(b"VDIREADY:", 200)
        self.pid = line.rsplit(b"VDIREADY:", 1)[1].strip().decode()
        if not self.pid:
            self.stop()
            raise EngineError("guestfish --listen produced no PID (appliance build failed?)")

    def stop(self) -> None:
        with self._lock:
            if self._p and self._p.poll() is None:
                try:
                    self._feed('guestfish --remote -- exit 2>/dev/null; exit\n')
                    self._p.wait(timeout=10)
                except Exception:
                    self._p.kill()
            self.pid = None

    def alive(self) -> bool:
        return bool(self._p) and self._p.poll() is None

    # -- plumbing ----------------------------------------------
    def _feed(self, s: str) -> None:
        self._p.stdin.write(s.encode())
        self._p.stdin.flush()

    def _read_until(self, needle: bytes, timeout: float) -> bytes:
        buf = bytearray()
        while needle not in bytes(buf):
            chunk = _read_chunk(self._p.stdout, timeout)
            if not chunk:
                raise EngineError(f"timeout waiting for {needle!r}; last output:\n"
                                  + bytes(buf).decode("utf-8", "replace")[-800:])
            buf += chunk
        return bytes(buf)

    def gf(self, *args: str, data_in: bytes | None = None, binary_out: bool = False,
           check: bool = True, timeout: float = 300) -> tuple[bytes, int]:
        """Run ``guestfish --remote -- <args>``. Returns (stdout, rc).

        ``data_in``: bytes that a ``-`` placeholder in *args* should read. Passed
        via a distro temp file (``guestfish --remote`` can't reliably take
        ``/dev/stdin``), so callers put a literal ``"-"`` where the temp path goes.
        """
        with self._lock:
            if not self.alive():
                raise EngineError("guestfish session is not running")
            tag = "VDI_" + _rand()
            tmp = None
            call_args = list(args)
            if data_in is not None:
                tmp = self.stage(data_in)
                call_args = [tmp if a == "-" else a for a in call_args]
            # Prefix the guestfish *command* with '-' so a command error never
            # terminates the --listen daemon (guestfish "EXIT ON ERROR BEHAVIOUR").
            # Cost: $? is then always 0, so errors are detected from the output.
            head = "-" + call_args[0]
            g = "guestfish --remote -- " + " ".join(
                shlex.quote(a) for a in [head, *call_args[1:]])
            if binary_out:
                script = f"{{ {g} ; }} | base64 -w0\nprintf '\\n{tag}\\n'\n"
            else:
                script = f"{g}\nprintf '\\n{tag}\\n'\n"
            _dbg("->", " ".join(args)[:120])
            self._feed(script)
            raw = self._read_until(f"\n{tag}\n".encode(), timeout)
            if tmp:
                _wsl(["rm", "-f", tmp], distro=self.distro, check=False)

        body = raw.rpartition(f"\n{tag}\n".encode())[0]
        if b"server is not running" in body or b"guestfish: remote:" in body:
            raise EngineError("guestfish --listen daemon is gone: "
                              + body.decode("utf-8", "replace").strip()[-300:])
        rc = 1 if b"libguestfs: error:" in body else 0
        _dbg("<-", f"rc={rc}", body[:200])
        if rc != 0 and check:
            raise GfError(f"guestfish {args[0]}: "
                          + body.decode("utf-8", "replace").strip()[-400:])
        if binary_out and rc == 0:
            return base64.b64decode(body.strip()), rc
        return body, rc

    def stage(self, data: bytes) -> str:
        """Write *data* to a distro temp file; return its path. Caller removes it
        (``gf`` does so automatically for its own staging)."""
        tmp = f"/tmp/vdi-{_rand()}"
        _wsl(["sh", "-c", f"base64 -d > {tmp}"], distro=self.distro,
             input_bytes=_b64(data).encode())
        return tmp

    def stage_tar(self, guest_folder: str, exclude: str | None = None) -> str:
        tmp = f"/tmp/vdi-{_rand()}.tar"
        ex = f"--exclude={shlex.quote(exclude)} " if exclude else ""
        _wsl(["sh", "-c", f"tar -C {shlex.quote(guest_folder)} {ex}-cf {tmp} ."],
             distro=self.distro)
        return tmp

    def rm_tmp(self, path: str) -> None:
        _wsl(["rm", "-f", path], distro=self.distro, check=False)

    def text(self, *args, **kw) -> str:
        return self.gf(*args, **kw)[0].decode("utf-8", "replace")

    def try_text(self, *args, **kw):
        out, rc = self.gf(*args, check=False, **kw)
        return (out.decode("utf-8", "replace"), rc)


class WslEngine(Engine):
    name = "wsl"

    _FS = {"fat16": "vfat", "fat32": "vfat", "vfat": "vfat", "exfat": "exfat",
           "ext2": "ext2", "ext3": "ext3", "ext4": "ext4"}
    # filesystems whose mkfs accepts a `label:` option (others must be labelled
    # after mkfs; passing label: to them makes guestfish kill the daemon)
    _MKFS_LABEL_OK = {"vfat", "ext2", "ext3", "ext4", "xfs", "btrfs"}

    def __init__(self, distro: str | None = None):
        self.distro = distro

    # -- probe ---------------------------------------------------
    def probe(self, *, deep: bool = False) -> EngineInfo:
        try:
            proc = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=15)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return EngineInfo(self.name, False, f"wsl.exe not usable: {exc}")
        distros = [d.strip() for d in proc.stdout.decode("utf-16-le", "replace").splitlines() if d.strip()]
        if not distros:
            return EngineInfo(self.name, False, "no WSL distro installed")
        if not _wsl(["sh", "-c", "command -v guestfish || true"],
                    distro=self.distro, check=False).stdout.strip():
            return EngineInfo(self.name, False, f"distros={distros}; guestfish MISSING -- in WSL: "
                              "sudo apt-get install -y libguestfs-tools qemu-utils xorriso")
        kern = _wsl(["sh", "-c", "ls /boot/vmlinuz-* 2>/dev/null | head -1"],
                    distro=self.distro, check=False).stdout.decode().strip()
        detail = f"distros={distros}; guestfish=yes"
        if not kern:
            return EngineInfo(self.name, False, detail + "; NO KERNEL for supermin -- in WSL: "
                              "sudo apt-get install -y linux-image-generic  (one-time, ~100MB)")
        readable = _wsl(["sh", "-c", f"test -r {shlex.quote(kern)} && echo y || echo n"],
                        distro=self.distro, check=False).stdout.decode().strip()
        if readable == "n":
            return EngineInfo(self.name, False, detail + f"; {kern} not readable by your user -- "
                              f"in WSL: sudo chmod 0644 /boot/vmlinuz-*")
        if deep:
            r = _wsl(["sh", "-c", "guestfish -a /dev/null run : quit"],
                     distro=self.distro, check=False, timeout=600)
            if r.returncode != 0:
                msg = (r.stdout or r.stderr or b"").decode("utf-8", "replace").strip()
                return EngineInfo(self.name, False, detail + "; appliance FAILED: " + msg[-400:])
            detail += "; appliance OK"
        return EngineInfo(self.name, True, detail, extra={"distros": distros})

    def supports_native_qemu_img(self) -> bool:
        return True

    def wsl_path(self, win_path: str) -> str:
        return win_to_wsl_path(win_path)

    # -- open --------------------------------------------------
    def open_image(self, image: str, partition: str | None = None, *,
                   readonly: bool = False, image_format: str | None = None) -> "WslOpenImage":
        return WslOpenImage(self, self.wsl_path(image), partition,
                            readonly=readonly, fmt=image_format or self._detect_fmt(image))

    def _detect_fmt(self, image: str) -> str | None:
        f = _fmt_of(image)
        if f:
            return f
        try:
            from vdi.image import QemuImg
            return QemuImg(engine=self).info(self.wsl_path(image)).get("format")
        except Exception:
            return None      # let guestfish auto-detect

    # -- build (需求 1) -------------------------------------
    def build_from_folder(self, folder: str, out: str, *, fmt: str, fs: str, size: str,
                          label: str = "", part_table: str = "auto",
                          boot: bool = False) -> None:
        if fs not in self._FS:
            raise EngineError(f"unsupported --fs {fs!r}")
        fstype = self._FS[fs]
        table = _resolve_part_table(part_table, fs)     # auto -> mbr for DOS fs
        gfolder, gout = self.wsl_path(folder), self.wsl_path(out)
        _wsl(["qemu-img", "create", "-f", fmt, gout, size], distro=self.distro, timeout=120)

        s = GuestfishSession(self.distro)
        s.start()
        try:
            s.gf("add-drive", gout, f"format:{fmt}")
            s.gf("run", timeout=400)
            s.gf("part-init", "/dev/sda", "mbr" if table == "mbr" else "gpt")
            s.gf("part-add", "/dev/sda", "primary", "2048", "-2048")
            dev = "/dev/sda1"

            mbr_id = _MBR_ID.get(fs)
            if table == "mbr" and mbr_id:
                # DOS / Windows 9x needs the right partition type byte + boot flag,
                # otherwise it reports the partition as "non-DOS" even for FAT32.
                s.gf("part-set-mbr-id", "/dev/sda", "1", mbr_id, check=False)
            if table == "mbr" and (boot or fs.startswith("fat")):
                s.gf("part-set-bootable", "/dev/sda", "1", "true", check=False)

            if fstype == "vfat":
                # set BPB hidden-sectors = partition LBA start so Win9x's fs
                # driver accepts the geometry; -F picks FAT16/32 by --fs.
                fbits = "16" if fs == "fat16" else "32"
                nm = f" -n {shlex.quote(label[:11])}" if label else ""
                r = s.gf("debug", "sh", f"mkfs.vfat -F {fbits} -h 2048{nm} {dev}", check=False)
                if r[1] != 0:
                    s.gf("mkfs", "vfat", dev)
            elif label and fstype in self._MKFS_LABEL_OK:
                s.gf("mkfs", fstype, dev, f"label:{label}")
            else:
                s.gf("mkfs", fstype, dev)
                if label:
                    _set_label_via_tool(s, dev, fstype, label)
            s.gf("mount", dev, "/")
            tar = s.stage_tar(gfolder, exclude=_inside(out, folder))
            try:
                s.gf("tar-in", tar, "/", timeout=600)
            finally:
                s.rm_tmp(tar)
            s.gf("umount-all")
            s.gf("shutdown", check=False)
        finally:
            s.stop()

    def build_iso(self, folder: str, out: str, *, volid: str = "", boot: str | None = None) -> None:
        args = ["xorriso", "-as", "mkisofs", "-R", "-J", "-joliet-long"]
        if volid:
            args += ["-V", volid]
        if boot:
            args += ["-b", boot, "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table"]
        args += ["-o", self.wsl_path(out), self.wsl_path(folder)]
        _wsl(args, distro=self.distro, timeout=300)


class WslOpenImage(OpenImage):
    def __init__(self, engine: WslEngine, guest_img: str, partition: str | None, *,
                 readonly: bool, fmt: str | None = None):
        self.engine = engine
        self.guest_img = guest_img
        self.readonly = readonly
        self.fmt = fmt
        self._s = GuestfishSession(engine.distro)
        self._s.start()
        args = ["add-drive", guest_img] + ([f"format:{fmt}"] if fmt else []) + \
               (["readonly:true"] if readonly else [])
        self._s.gf(*args)
        with log.waiting("wsl: booting appliance VM"):
            self._s.gf("run", timeout=400)
        self._mount(partition)

    # test/debug hook used by `vdi image parts`
    def _remote(self, *a):
        out, _ = self._s.gf(*a, check=False)
        return type("R", (), {"stdout": out})()

    def _mount(self, partition):
        dev = self._resolve_partition(partition)
        log.step(f"wsl: mounting {dev}")
        _, rc = self._s.gf("mount-ro" if self.readonly else "mount", dev, "/", check=False)
        if rc != 0:
            raise EngineError(
                f"could not mount {dev}. If the image is blank, format it first:\n"
                f"    vdi image create <folder> {os.path.basename(self.guest_img)} --fs ext4 --size 1G")
        self.fs = self._s.text("vfs-type", dev).strip() or "unknown"
        self._device = dev
        log.step(f"wsl: mounted {dev}  fs={self.fs}")

    def _resolve_partition(self, partition):
        pairs = []
        for line in self._s.text("list-filesystems").splitlines():
            if ":" in line:
                d, t = (x.strip() for x in line.split(":", 1))
                pairs.append((d, t))
        log.trace(f"wsl: filesystems = {pairs}")
        if partition is None:
            for d, t in pairs:
                if t not in _SWAP_OR_UNKNOWN:
                    return d
            # nothing recognised -- maybe a whole-disk fs list-filesystems missed
            for d, t in pairs:
                _, rc = self._s.gf("file", d, check=False)
                blk, brc = self._s.try_text("vfs-type", d)
                if brc == 0 and blk.strip() and blk.strip() != "unknown":
                    return d
            raise EngineError(
                "the image has no partition table and no recognised filesystem "
                f"(devices: {pairs}). Is it a blank image? Format one with:\n"
                f"    vdi image create <folder> {os.path.basename(self.guest_img)} --fs ext4 --size 1G")
        if partition.startswith("/dev/"):
            return partition
        if partition.isdigit():
            return f"/dev/sda{partition}"
        out, rc = self._s.try_text("findfs-label", partition)
        if rc == 0 and out.strip():
            return out.strip()
        raise EngineError(f"cannot resolve partition {partition!r}; filesystems={pairs}")

    # -- lifecycle -------------------------------------------
    def open(self, *a, **k):
        pass

    def close(self):
        try:
            if self._s.alive():
                self._s.gf("umount-all", check=False, timeout=30)
                self._s.gf("shutdown", check=False, timeout=60)
        finally:
            self._s.stop()

    def df(self) -> DfInfo:
        f = _parse_struct(self._s.text("statvfs", "/"))
        bsize = int(f.get("bsize") or f.get("frsize") or 4096)
        total = int(f.get("blocks", 0)) * bsize
        free = int(f.get("bavail", f.get("bfree", 0))) * bsize
        return DfInfo(self.fs, total, total - free, free)

    # -- read ----------------------------------------------
    def ls(self, path, *, long=False, recursive=False):
        out, rc = self._s.try_text("find" if recursive else "ls", path)
        if rc != 0:
            if self._exists(path):          # a file, not a dir
                return [self._entry(path, display=path.rsplit("/", 1)[-1] or "/")]
            raise NotFound(path)
        entries = []
        for n in out.splitlines():
            if n in ("", "."):
                continue
            rel = n.lstrip("/")             # `find` yields entries relative to `path`
            full = _join(path, rel)
            entries.append(self._entry(full, display=full if recursive else rel))
        return entries

    def _exists(self, path) -> bool:
        out, _ = self._s.try_text("exists", path)
        return out.strip() == "true"

    def _entry(self, full, *, display=None):
        st = self.stat(full)
        return DirEntry(name=display if display is not None else (full.rsplit("/", 1)[-1] or "/"),
                        type=st.type, size=st.size, mtime=st.mtime, mode=st.mode)

    def stat(self, path) -> StatInfo:
        out, rc = self._s.try_text("statns", path)
        if rc != 0:
            raise NotFound(path)
        f = _parse_struct(out)
        mode = int(f.get("st_mode", 0))
        return StatInfo(
            type=_type_from_mode(mode), size=int(f.get("st_size", 0)),
            mode=oct(mode & 0o7777)[2:].rjust(4, "0"),
            uid=int(f.get("st_uid", 0)), gid=int(f.get("st_gid", 0)),
            atime=int(f.get("st_atime_sec", 0)), mtime=int(f.get("st_mtime_sec", 0)),
            ctime=int(f.get("st_ctime_sec", 0)), nlink=int(f.get("st_nlink", 1)),
            inode=int(f.get("st_ino", 0)))

    def read(self, path, offset=0, length=None) -> bytes:
        if length is None:
            data, rc = self._s.gf("download", path, "/dev/stdout", binary_out=True, check=False)
            if rc != 0:
                raise NotFound(path)
            return data
        data, rc = self._s.gf("pread", path, str(length), str(offset), binary_out=True, check=False)
        if rc != 0:
            raise NotFound(path)
        return data

    def tree_size(self, path, *, apparent=False) -> int:
        out, rc = self._s.try_text("du", path)
        if rc != 0:
            raise NotFound(path)
        return int(out.strip() or 0) * 1024

    # -- write ---------------------------------------------
    def _guard_ro(self):
        if self.readonly:
            raise ReadOnly()

    def write(self, path, data: bytes, offset=0, *, append=False):
        self._guard_ro()
        parent = path.rsplit("/", 1)[0]
        if parent:
            self._s.gf("mkdir-p", parent, check=False)
        if append:
            _, rc = self._s.try_text("is-file", path)
            base = self.read(path) if rc == 0 else b""
            data, offset = base + data, 0
        tmp = self._s.stage(data)
        try:
            if offset:
                self._s.gf("upload-offset", tmp, path, str(offset))
            else:
                self._s.gf("upload", tmp, path)
        finally:
            self._s.rm_tmp(tmp)

    def mkdir(self, path, *, parents=False):
        self._guard_ro()
        self._s.gf("mkdir-p" if parents else "mkdir", path)

    def rmdir(self, path, *, recursive=False):
        self._guard_ro()
        self._s.gf("rm-rf" if recursive else "rmdir", path)

    def rm(self, path):
        self._guard_ro()
        self._s.gf("rm", path)

    def rename(self, src, dst):
        self._guard_ro()
        self._s.gf("mv", src, dst)

    def chmod(self, path, mode: int):
        self._guard_ro()
        if self.fs in NO_POSIX_PERMS:
            raise Unsupported(f"{self.fs} has no POSIX permissions")
        self._s.gf("chmod", oct(mode)[2:], path)

    def chown(self, path, uid, gid):
        self._guard_ro()
        if self.fs in NO_POSIX_PERMS:
            raise Unsupported(f"{self.fs} has no POSIX ownership")
        self._s.gf("chown", str(uid), str(gid), path)

    # -- bulk --------------------------------------------
    def upload_tree(self, local_path, dst):
        self._guard_ro()
        gl = self.engine.wsl_path(local_path)
        self._s.gf("mkdir-p", dst)
        tar = self._s.stage_tar(gl)
        try:
            self._s.gf("tar-in", tar, dst, timeout=600)
        finally:
            self._s.rm_tmp(tar)

    def grep(self, pattern, path, *, glob=None, ignore_case=False, max_results=1000):
        from vdi.fsops import GrepHit
        flags = "-rnI" + ("i" if ignore_case else "")
        inc = f" --include={shlex.quote(glob)}" if glob else ""
        sysp = "/sysroot" + (path if path.startswith("/") else "/" + path)
        cmd = (f"grep {flags}{inc} -e {shlex.quote(pattern)} {shlex.quote(sysp)} "
               f"2>/dev/null | head -n {int(max_results)} || true")
        out, _ = self._s.try_text("debug", "sh", cmd)
        hits = []
        for line in out.splitlines():
            rest = line[len("/sysroot"):] if line.startswith("/sysroot") else line
            p, _, r = rest.partition(":")
            n, _, txt = r.partition(":")
            if n.isdigit():
                hits.append(GrepHit(path=p, line=int(n), text=txt[:500]))
        return hits

    def download_tree(self, src, local_path):
        data, rc = self._s.gf("tar-out", src, "/dev/stdout", binary_out=True, check=False)
        if rc != 0:
            raise NotFound(src)
        os.makedirs(local_path, exist_ok=True)
        gl = self.engine.wsl_path(local_path)
        _wsl(["sh", "-c", f"base64 -d | tar -C {shlex.quote(gl)} -xf -"],
             distro=self.engine.distro, input_bytes=_b64(data).encode())


# -- helpers ---------------------------------------------------
def win_to_wsl_path(p: str) -> str:
    """Translate a Windows path to its /mnt/<drive> WSL form, in pure Python.

    Calling ``wslpath`` via ``wsl.exe --`` mangles backslashes, so we don't.
    """
    p = os.path.abspath(p)
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    if p.startswith("\\\\wsl$") or p.startswith("\\\\wsl.localhost"):
        # \\wsl$\Distro\home\... -> /home/...
        parts = p.replace("\\", "/").split("/")
        return "/" + "/".join(parts[4:])
    return p.replace("\\", "/")


def _set_label_via_tool(s: "GuestfishSession", dev: str, fstype: str, label: str) -> None:
    """Label exFAT/NTFS/FAT via their own tools in the appliance. guestfish's
    own ``set-label`` / ``mkfs label:`` raise a *fatal* 'don't know how' error
    for these, which kills the --listen daemon -- so route around it."""
    tool = {"exfat": "exfatlabel", "ntfs": "ntfslabel", "vfat": "fatlabel",
            "fat": "fatlabel"}.get(fstype)
    if not tool:
        return
    cmd = f"{tool} {shlex.quote(dev)} {shlex.quote(label)}"
    out, rc = s.gf("debug", "sh", cmd, check=False)
    if rc != 0:
        # non-fatal: label is cosmetic
        pass


# MBR partition type bytes so DOS / Windows recognise the volume
_MBR_ID = {"fat16": "0x0e", "fat32": "0x0c", "exfat": "0x07"}
_DOS_FS = {"fat16", "fat32", "exfat"}


def _resolve_part_table(choice: str, fs: str) -> str:
    if choice in ("mbr", "dos", "msdos"):
        return "mbr"
    if choice == "gpt":
        return "gpt"
    # auto: MBR for DOS/Windows filesystems, GPT for Linux ones
    return "mbr" if fs in _DOS_FS else "gpt"


def _inside(out: str, folder: str) -> str | None:
    """If ``out`` is a file inside ``folder``, return its ``./rel`` path for tar
    --exclude; else None. Cross-drive / unrelated paths are None."""
    try:
        rel = os.path.relpath(os.path.abspath(out), os.path.abspath(folder))
    except ValueError:
        return None
    return None if rel.startswith("..") else "./" + rel.replace(os.sep, "/")


def _fmt_of(image: str) -> str | None:
    low = image.lower()
    for ext, f in ((".vmdk", "vmdk"), (".vhdx", "vhdx"), (".vhd", "vpc"),
                   (".qcow2", "qcow2"), (".raw", "raw"), (".img", "raw")):
        if low.endswith(ext):
            return f
    return None


def _parse_struct(text: str) -> dict:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _type_from_mode(mode: int) -> str:
    return {0o040000: "dir", 0o100000: "file", 0o120000: "symlink"}.get(mode & 0o170000, "other")


def _join(base: str, name: str) -> str:
    if name.startswith("/"):
        return name
    return (base.rstrip("/") + "/" + name) if base != "/" else "/" + name
