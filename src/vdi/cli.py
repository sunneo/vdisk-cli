"""``vdi`` command-line entry point (argparse, stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from vdi import __version__, registry
from vdi.errors import VdiError
from vdi.target import parse_target, parse_session_target


# ----------------------------------------------------------------------
# shared: obtain a FilesystemOps for a target, one-shot or via a session
# ----------------------------------------------------------------------
@contextlib.contextmanager
def open_ops(target: str | None, *, session: str | None, engine: str,
             readonly: bool, session_form: bool = False):
    from vdi.rpc import RpcClient
    from vdi.remote import RemoteOps

    if session or session_form:
        if session_form:
            st = parse_session_target(target)
            sess = registry.resolve(st.session)
            inner = st.path
        else:
            sess = registry.resolve(session)
            inner = parse_target(target).path if target else "/"
        yield RemoteOps(RpcClient(sess.rpc.addr, sess.rpc.port, sess.rpc.token)), inner
        return

    tgt = parse_target(target)

    # If a live session already holds this exact image, reuse it -- avoids the
    # ~50s appliance boot for every one-shot command.
    if os.environ.get("VDI_NO_REUSE") != "1":
        want = os.path.abspath(tgt.image)
        for s in registry.list_sessions():
            if os.path.abspath(s.image) == want and (not readonly or s.readonly or True):
                yield RemoteOps(RpcClient(s.rpc.addr, s.rpc.port, s.rpc.token)), tgt.path
                return

    from vdi.engine import get_engine
    eng = get_engine(engine)
    with eng.open_image(tgt.image, tgt.partition, readonly=readonly) as img:
        yield img, tgt.path


# ----------------------------------------------------------------------
# doctor
# ----------------------------------------------------------------------
def cmd_doctor(args) -> int:
    import shutil
    from vdi.engine import available_engines

    print(f"vdi {__version__}\n")
    print("host tools:")
    for tool in ("qemu-img", "qemu-system-x86_64", "xorriso", "wsl.exe"):
        print(f"  {tool:22} {shutil.which(tool) or '(not found)'}")
    print("\nengines:")
    for info in available_engines():
        mark = "OK  " if info.available else "--  "
        print(f"  {mark}{info.name:6} {info.detail}")
    print(f"\nregistry: {registry.registry_dir()}")
    live = registry.list_sessions()
    print(f"live sessions: {len(live)}")
    for s in live:
        print(f"  {s.name}  {s.image}  fs={s.fs_type}  rpc={s.rpc.addr}:{s.rpc.port}")
    return 0


# ----------------------------------------------------------------------
# image
# ----------------------------------------------------------------------
def cmd_image_create(args) -> int:
    from vdi.image import fmt_from_path
    from vdi.engine import get_engine

    src = Path(args.folder)
    if not src.is_dir():
        raise VdiError(f"source folder not found: {src}")
    out = Path(args.output)
    if out.exists() and not args.force:
        raise VdiError(f"{out} exists; pass --force")

    eng = get_engine(args.engine)

    if out.suffix.lower() == ".iso":
        if not hasattr(eng, "build_iso"):
            raise VdiError(f"engine {eng.name!r} cannot build ISOs; try --engine wsl")
        print(f"[vdi] building ISO {out} from {src}")
        eng.build_iso(str(src), str(out), volid=args.label)
        print("[vdi] done")
        return 0

    fmt = fmt_from_path(str(out))
    if not hasattr(eng, "build_from_folder"):
        raise VdiError(f"engine {eng.name!r} cannot build disk images; try --engine wsl")
    print(f"[vdi] building {fmt} {out} ({args.size}) fs={args.fs} label={args.label or '-'} "
          f"part-table={args.part_table}")
    eng.build_from_folder(str(src), str(out), fmt=fmt, fs=args.fs, size=args.size,
                          label=args.label, part_table=args.part_table)
    print("[vdi] done")
    return 0


def cmd_image_convert(args) -> int:
    from vdi.image import QemuImg, fmt_from_path
    from vdi.engine import get_engine

    out = Path(args.output)
    if out.exists() and not args.force:
        raise VdiError(f"{out} exists; pass --force")
    qi = QemuImg(engine=get_engine(args.engine))
    print(f"[vdi] converting {args.input} -> {out}")
    qi.convert(args.input, str(out), compress=args.compress,
               subformat=args.subformat, preallocation=args.preallocation)
    print("[vdi] qemu-img check ...")
    qi.check(str(out))
    print("[vdi] done")
    return 0


def cmd_image_info(args) -> int:
    from vdi.image import QemuImg
    from vdi.engine import get_engine
    qi = QemuImg(engine=get_engine(args.engine))
    info = qi.info(args.image)
    print(json.dumps(info, indent=2))
    return 0


def cmd_image_parts(args) -> int:
    from vdi.engine import get_engine
    tgt = parse_target(args.image)
    eng = get_engine(args.engine)
    with eng.open_image(tgt.image, None, readonly=True) as img:
        # skeleton: list-filesystems via the engine handle if it exposes it
        lister = getattr(img, "_remote", None)
        if lister:
            print(lister("list-filesystems").stdout.decode().strip())
        else:
            print(f"fs_type={img.fs_type()}")
    return 0


# ----------------------------------------------------------------------
# fs
# ----------------------------------------------------------------------
def _fs_common(p: argparse.ArgumentParser):
    p.add_argument("--session", help="operate via a running 'vdi serve' session")
    p.add_argument("--engine", default="auto", help="auto|wsl|qemu")
    p.add_argument("--readonly", action="store_true")


def _open_for_fs(args, target_pos: str):
    from vdi.target import looks_like_session_target
    session_form = args.session is None and looks_like_session_target(target_pos)
    return open_ops(target_pos, session=args.session, engine=args.engine,
                    readonly=args.readonly, session_form=session_form)


def cmd_fs_ls(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        entries = ops.ls(path, long=args.long, recursive=args.recursive)
        if args.json:
            print(json.dumps([e.dict() for e in entries], indent=2))
        else:
            for e in entries:
                if args.long:
                    print(f"{e.mode}  {e.type[:1]}  {e.size:>12}  {e.mtime:>11}  {e.name}")
                else:
                    print(e.name)
    return 0


def cmd_fs_stat(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        st = ops.stat(path)
        print(json.dumps(st.dict(), indent=2) if args.json else _fmt_stat(st, path))
    return 0


def cmd_fs_size(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        print(ops.tree_size(path, apparent=args.apparent))
    return 0


def cmd_fs_read(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        data = ops.read(path, args.offset, args.length)
        if args.output:
            Path(args.output).write_bytes(data)
        else:
            sys.stdout.buffer.write(data)
    return 0


def cmd_fs_write(args) -> int:
    if args.content is not None:
        data = args.content.encode()
    elif args.input:
        data = Path(args.input).read_bytes()
    else:
        data = sys.stdin.buffer.read()
    with _open_for_fs(args, args.target) as (ops, path):
        ops.write(path, data, append=args.append)
    return 0


def cmd_fs_mkdir(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        ops.mkdir(path, parents=args.parents)
    return 0


def cmd_fs_rmdir(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        ops.rmdir(path, recursive=args.recursive)
    return 0


def cmd_fs_rm(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        ops.rm(path)
    return 0


def cmd_fs_rename(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        dst = parse_target(args.dst).path if ":" in args.dst else args.dst
        ops.rename(path, dst)
    return 0


def cmd_fs_cp_in(args) -> int:
    with _open_for_fs(args, args.dst) as (ops, path):
        ops.upload_tree(args.src, path)
    return 0


def cmd_fs_cp_out(args) -> int:
    with _open_for_fs(args, args.src) as (ops, path):
        ops.download_tree(path, args.dst)
    return 0


def cmd_fs_chmod(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        ops.chmod(path, int(args.mode, 8))
    return 0


def cmd_fs_chown(args) -> int:
    uid, _, gid = args.owner.partition(":")
    with _open_for_fs(args, args.target) as (ops, path):
        ops.chown(path, int(uid), int(gid or uid))
    return 0


def cmd_fs_df(args) -> int:
    with _open_for_fs(args, args.target) as (ops, _):
        print(json.dumps(ops.df().dict(), indent=2))
    return 0


def cmd_fs_grep(args) -> int:
    with _open_for_fs(args, args.target) as (ops, path):
        hits = ops.grep(args.pattern, path, glob=args.glob, ignore_case=args.ignore_case,
                        max_results=args.max_results)
        if args.json:
            print(json.dumps([h.dict() for h in hits], indent=2))
        else:
            for h in hits:
                print(f"{h.path}:{h.line}:{h.text}")
    return 0


# ----------------------------------------------------------------------
# serve / sessions / mcp
# ----------------------------------------------------------------------
def cmd_serve(args) -> int:
    from vdi.daemon import Daemon
    addr, _, port = (args.rpc or "127.0.0.1:0").rpartition(":")
    d = Daemon(args.image, name=args.name, partition=args.partition,
               readonly=args.readonly, engine=args.engine,
               rpc_addr=addr or "127.0.0.1", rpc_port=int(port or 0),
               idle_timeout=_parse_duration(args.idle_timeout),
               mcp=args.mcp, mcp_writable=args.mcp_writable, mcp_root=args.mcp_root,
               ftp=args.ftp, webdav=args.webdav, mount=args.mount)
    return d.run()


def cmd_sessions(args) -> int:
    live = registry.list_sessions()
    if args.json:
        print(json.dumps([json.loads(s.to_json()) for s in live], indent=2))
        return 0
    if not live:
        print("(no live sessions)")
    for s in live:
        print(f"{s.name:16} {s.fs_type:8} {'ro' if s.readonly else 'rw'}  "
              f"{s.rpc.addr}:{s.rpc.port}  {s.image}")
    return 0


def cmd_session_stop(args) -> int:
    import os
    import signal
    s = registry.resolve(args.name)
    try:
        os.kill(s.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        print(f"could not signal pid {s.pid}: {e}", file=sys.stderr)
    return 0


def cmd_session_info(args) -> int:
    s = registry.resolve(args.name)
    print(s.to_json())
    return 0


def cmd_mcp(args) -> int:
    from vdi.engine import get_engine
    from vdi.mcp_server import McpTools, run_stdio

    from vdi.target import looks_like_session_target
    session_form = looks_like_session_target(args.target)
    if session_form:
        from vdi.rpc import RpcClient
        from vdi.remote import RemoteOps
        st = parse_session_target(args.target)
        sess = registry.resolve(st.session)
        ops = RemoteOps(RpcClient(sess.rpc.addr, sess.rpc.port, sess.rpc.token))
        tools = McpTools(ops, writable=args.writable, root=args.root)
        return run_stdio(tools)

    tgt = parse_target(args.target)
    eng = get_engine(args.engine)
    with eng.open_image(tgt.image, tgt.partition, readonly=not args.writable) as img:
        tools = McpTools(img, writable=args.writable, root=args.root)
        return run_stdio(tools)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _fmt_stat(st, path) -> str:
    return "\n".join([
        f"path   {path}", f"type   {st.type}", f"size   {st.size}",
        f"mode   {st.mode}", f"owner  {st.uid}:{st.gid}",
        f"mtime  {st.mtime}", f"atime  {st.atime}", f"ctime  {st.ctime}",
        f"nlink  {st.nlink}", f"inode  {st.inode}",
    ])


def _parse_duration(s: str | None) -> float | None:
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600}
    if s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vdi", description=__doc__)
    p.add_argument("--version", action="version", version=f"vdi {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check engines and environment").set_defaults(func=cmd_doctor)

    # image ---------------------------------------------------------
    img = sub.add_parser("image", help="image-level operations").add_subparsers(dest="sub", required=True)

    c = img.add_parser("create", help="build an image from a folder")
    c.add_argument("folder"); c.add_argument("output")
    c.add_argument("--fs", default="ext4", choices=list(("fat16 fat32 exfat ext2 ext3 ext4").split()))
    c.add_argument("--size", default="1G")
    c.add_argument("--label", default="")
    c.add_argument("--part-table", default="gpt", choices=["gpt", "mbr"])
    c.add_argument("--engine", default="auto")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_image_create)

    c = img.add_parser("convert", help="convert between disk image formats")
    c.add_argument("input"); c.add_argument("output")
    c.add_argument("--compress", action="store_true")
    c.add_argument("--subformat")
    c.add_argument("--preallocation", choices=["off", "metadata", "full"])
    c.add_argument("--engine", default="auto")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_image_convert)

    c = img.add_parser("info", help="show image format/size")
    c.add_argument("image"); c.add_argument("--engine", default="auto")
    c.set_defaults(func=cmd_image_info)

    c = img.add_parser("parts", help="list partitions / filesystems")
    c.add_argument("image"); c.add_argument("--engine", default="auto")
    c.set_defaults(func=cmd_image_parts)

    # fs ------------------------------------------------------------
    fs = sub.add_parser("fs", help="CRUD on files inside an image").add_subparsers(dest="sub", required=True)

    c = fs.add_parser("ls"); c.add_argument("target")
    c.add_argument("-l", "--long", action="store_true")
    c.add_argument("-a", "--all", action="store_true")
    c.add_argument("-R", "--recursive", action="store_true")
    c.add_argument("--json", action="store_true"); _fs_common(c); c.set_defaults(func=cmd_fs_ls)

    c = fs.add_parser("stat"); c.add_argument("target"); c.add_argument("--json", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_stat)

    c = fs.add_parser("size"); c.add_argument("target"); c.add_argument("--apparent", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_size)

    c = fs.add_parser("read"); c.add_argument("target"); c.add_argument("-o", "--output")
    c.add_argument("--offset", type=int, default=0); c.add_argument("--length", type=int)
    _fs_common(c); c.set_defaults(func=cmd_fs_read)

    c = fs.add_parser("write"); c.add_argument("target")
    c.add_argument("-i", "--input"); c.add_argument("--content"); c.add_argument("--stdin", action="store_true")
    c.add_argument("--append", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_write)

    c = fs.add_parser("mkdir"); c.add_argument("target"); c.add_argument("-p", "--parents", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_mkdir)

    c = fs.add_parser("rmdir"); c.add_argument("target"); c.add_argument("-r", "--recursive", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_rmdir)

    c = fs.add_parser("rm"); c.add_argument("target"); _fs_common(c); c.set_defaults(func=cmd_fs_rm)

    c = fs.add_parser("rename"); c.add_argument("target"); c.add_argument("dst")
    _fs_common(c); c.set_defaults(func=cmd_fs_rename)

    c = fs.add_parser("mv"); c.add_argument("target"); c.add_argument("dst")
    _fs_common(c); c.set_defaults(func=cmd_fs_rename)

    c = fs.add_parser("cp-in"); c.add_argument("src"); c.add_argument("dst")
    c.add_argument("-r", "--recursive", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_cp_in)

    c = fs.add_parser("cp-out"); c.add_argument("src"); c.add_argument("dst")
    c.add_argument("-r", "--recursive", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_cp_out)

    c = fs.add_parser("chmod"); c.add_argument("target"); c.add_argument("mode")
    _fs_common(c); c.set_defaults(func=cmd_fs_chmod)

    c = fs.add_parser("chown"); c.add_argument("target"); c.add_argument("owner")
    _fs_common(c); c.set_defaults(func=cmd_fs_chown)

    c = fs.add_parser("df"); c.add_argument("target"); _fs_common(c); c.set_defaults(func=cmd_fs_df)

    c = fs.add_parser("grep"); c.add_argument("pattern"); c.add_argument("target")
    c.add_argument("--glob"); c.add_argument("-i", "--ignore-case", action="store_true")
    c.add_argument("--max-results", type=int, default=1000)
    c.add_argument("--json", action="store_true")
    _fs_common(c); c.set_defaults(func=cmd_fs_grep)

    # serve / sessions / mcp -------------------------------------
    c = sub.add_parser("serve", help="hold an image open and expose it over RPC")
    c.add_argument("image")
    c.add_argument("--name"); c.add_argument("--partition")
    c.add_argument("--readonly", action="store_true")
    c.add_argument("--engine", default="auto")
    c.add_argument("--rpc", default="127.0.0.1:0")
    c.add_argument("--mcp", help="also expose MCP on this bind (host:port)")
    c.add_argument("--mcp-writable", action="store_true", help="let MCP clients modify files")
    c.add_argument("--mcp-root", default="/", help="restrict frontends to this subtree")
    c.add_argument("--ftp", nargs="?", const="2121", help="expose FTP (host:port, default :2121)")
    c.add_argument("--webdav", nargs="?", const="8080", help="expose WebDAV (host:port, default :8080)")
    c.add_argument("--mount", help="FUSE mount point (Linux/macOS)")
    c.add_argument("--idle-timeout")
    c.set_defaults(func=cmd_serve)

    c = sub.add_parser("sessions", help="list live sessions")
    c.add_argument("--json", action="store_true"); c.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("session", help="inspect/stop a session").add_subparsers(dest="sub", required=True)
    x = sp.add_parser("info"); x.add_argument("name"); x.set_defaults(func=cmd_session_info)
    x = sp.add_parser("stop"); x.add_argument("name"); x.set_defaults(func=cmd_session_stop)

    c = sub.add_parser("mcp", help="serve one image to an AI over MCP (stdio)")
    c.add_argument("target")
    c.add_argument("--engine", default="auto")
    c.add_argument("--writable", action="store_true", help="allow the AI to modify files")
    c.add_argument("--root", default="/", help="restrict the AI to this subtree")
    c.set_defaults(func=cmd_mcp)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except VdiError as e:
        print(f"vdi: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"vdi: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
