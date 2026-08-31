"""``vdi gui`` -- a small tkinter file manager for virtual disk images.

Stdlib only (tkinter ships with Python; on Linux install the distro's
``python3-tk``). Open an image with any engine, or attach to a running
``vdi serve`` session, then browse / read / write / import / export files.

All filesystem work runs on one background worker thread (matching the
single-writer model), so the UI never blocks on an engine boot.
"""
from __future__ import annotations

import os
import queue
import threading
import traceback
from dataclasses import dataclass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from vdi import __version__, registry
from vdi.target import parse_target, normalize_inner


# ----------------------------------------------------------------------
# background worker: serialize every op, keep Tk responsive
# ----------------------------------------------------------------------
class Worker:
    """One background thread runs the jobs; Tk is touched only from the main
    thread, which polls a result queue. (tkinter is not thread-safe.)"""

    def __init__(self, root: "VdiGui"):
        self.root = root
        self._in: "queue.Queue" = queue.Queue()
        self._out: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()
        self.root.after(40, self._pump)

    def submit(self, label, fn, on_done=None, on_error=None):
        self._out.put(("busy", label))
        self._in.put((label, fn, on_done, on_error))

    def _loop(self):
        while True:
            label, fn, on_done, on_error = self._in.get()
            try:
                self._out.put(("done", on_done, fn()))
            except Exception as e:
                self._out.put(("error", on_error, e, traceback.format_exc()))
            self._out.put(("idle", None))

    def _pump(self):
        try:
            while True:
                msg = self._out.get_nowait()
                kind = msg[0]
                if kind == "busy":
                    self.root._busy(True, msg[1])
                elif kind == "idle":
                    self.root._busy(False, "")
                elif kind == "done":
                    if msg[1]:
                        msg[1](msg[2])
                elif kind == "error":
                    (msg[1] or self._default_error)(msg[2], msg[3])
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _default_error(self, e, tb):
        messagebox.showerror("vdi", f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------
@dataclass
class Conn:
    ops: object
    label: str
    readonly: bool
    close: object          # callable


# ----------------------------------------------------------------------
class VdiGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"vdi {__version__}")
        self.geometry("900x560")
        self.minsize(640, 400)

        self.worker = Worker(self)
        self.conn: Conn | None = None
        self.cwd = "/"
        self.busy = False

        self._build_menu()
        self._build_toolbar()
        self._build_tree()
        self._build_status()

        self.protocol("WM_DELETE_WINDOW", self._quit)

        self._toggle(True)
        self._refresh_sessions_menu()

    # -- layout -------------------------------------------------------
    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open image…", command=self.open_image_dialog)
        self.sess_menu = tk.Menu(fm, tearoff=0)
        fm.add_cascade(label="Attach to session", menu=self.sess_menu)
        fm.add_separator()
        fm.add_command(label="Create image from folder…", command=self.create_dialog)
        fm.add_command(label="Convert image…", command=self.convert_dialog)
        fm.add_separator()
        fm.add_command(label="Close", command=self.close_conn)
        fm.add_command(label="Quit", command=self._quit)
        m.add_cascade(label="File", menu=fm)

        sm = tk.Menu(m, tearoff=0)
        sm.add_command(label="Serve this image…", command=self.serve_dialog)
        sm.add_command(label="Refresh session list", command=self._refresh_sessions_menu)
        m.add_cascade(label="Server", menu=sm)
        self.config(menu=m)

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=3)
        self.tb = {}
        for key, text, cmd in [
            ("up", "↑ Up", self.go_up),
            ("refresh", "↻ Refresh", self.refresh),
            ("mkdir", "New Folder", self.mkdir),
            ("rename", "Rename", self.rename),
            ("delete", "Delete", self.delete),
            ("import", "Import…", self.import_path),
            ("export", "Export…", self.export_path),
            ("view", "View / Edit", self.view_file),
        ]:
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self.tb[key] = b
        self.path_var = tk.StringVar(value="/")
        pe = ttk.Entry(bar, textvariable=self.path_var)
        pe.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        pe.bind("<Return>", lambda e: self.navigate(self.path_var.get()))

    def _build_tree(self):
        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=4)
        cols = ("size", "type", "mtime", "mode")
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Name")
        self.tree.column("#0", width=340, anchor=tk.W)
        for c, w in zip(cols, (110, 80, 160, 70)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_double)

    def _build_status(self):
        self.status = ttk.Label(self, text="no image open", anchor=tk.W, relief=tk.SUNKEN)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    def _busy(self, b: bool, label: str):
        self.busy = b
        self.config(cursor="watch" if b else "")
        if label:
            self.status.config(text=label)
        elif not b:
            self.status.config(text=self.cwd if self.conn else "ready")
        self._toggle(not b)

    def _toggle(self, enabled: bool):
        state = "normal" if (enabled and self.conn) else "disabled"
        for k, b in self.tb.items():
            b.config(state=state)
        if not self.conn:
            self.status.config(text="no image open  —  File ▸ Open image")

    # -- connection -------------------------------------------------
    def open_image_dialog(self):
        path = filedialog.askopenfilename(
            title="Open disk image",
            filetypes=[("Disk images", "*.vmdk *.vhdx *.vhd *.qcow2 *.img *.raw *.iso"),
                       ("All files", "*.*")])
        if not path:
            return
        d = _AskOpts(self, path)
        self.wait_window(d)
        if not d.ok:
            return
        eng_name, part, ro = d.engine, d.partition or None, d.readonly

        fmt = d.fmt if d.fmt != "auto" else None

        def work():
            from vdi.engine import get_engine
            eng = get_engine(eng_name)
            img = eng.open_image(path, part, readonly=ro, image_format=fmt)
            return Conn(img, f"{os.path.basename(path)} [{img.fs_type()}]", ro, img.close)

        self.worker.submit(f"opening {os.path.basename(path)} …", work, self._connected)

    def attach_session(self, name):
        def work():
            from vdi.remote import RemoteOps
            from vdi.rpc import RpcClient
            s = registry.resolve(name)
            ops = RemoteOps(RpcClient(s.rpc.addr, s.rpc.port, s.rpc.token))
            return Conn(ops, f"session {name} [{s.fs_type}]", s.readonly, lambda: None)
        self.worker.submit(f"attaching to {name} …", work, self._connected)

    def _connected(self, conn: Conn):
        self.close_conn(silent=True)
        self.conn = conn
        self.cwd = "/"
        self.title(f"vdi — {conn.label}")
        self.refresh()

    def close_conn(self, silent=False):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.title(f"vdi {__version__}")
        self.tree.delete(*self.tree.get_children())
        self._toggle(True)

    def _refresh_sessions_menu(self):
        self.sess_menu.delete(0, tk.END)
        live = registry.list_sessions()
        if not live:
            self.sess_menu.add_command(label="(no live sessions)", state="disabled")
        for s in live:
            self.sess_menu.add_command(label=f"{s.name}  —  {s.image}",
                                       command=lambda n=s.name: self.attach_session(n))

    # -- browsing --------------------------------------------------
    def refresh(self):
        if not self.conn:
            return
        self.path_var.set(self.cwd)
        path = self.cwd
        ro = self.conn.readonly

        def work():
            entries = self.conn.ops.ls(path, long=True)
            df = None
            try:
                df = self.conn.ops.df()
            except Exception:
                pass
            return entries, df

        def done(res):
            entries, df = res
            self.tree.delete(*self.tree.get_children())
            for e in sorted(entries, key=lambda x: (x.type != "dir", x.name.lower())):
                icon = "\U0001F4C1 " if e.type == "dir" else "\U0001F4C4 "
                self.tree.insert("", tk.END, iid=e.name, text=icon + e.name,
                                 values=(_human(e.size) if e.type != "dir" else "",
                                         e.type, _ts(e.mtime), e.mode))
            extra = ""
            if df:
                extra = f"   {df.fs_type}   {_human(df.used_bytes)}/{_human(df.total_bytes)} used"
            self.status.config(text=f"{self.cwd}{'   (read-only)' if ro else ''}{extra}")

        self.worker.submit(f"listing {path} …", work, done)

    def _on_double(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        name = sel[0]
        vals = self.tree.item(name, "values")
        if vals and vals[1] == "dir":
            self.navigate(_join(self.cwd, name))
        else:
            self.view_file()

    def navigate(self, path):
        self.cwd = normalize_inner(path)
        self.refresh()

    def go_up(self):
        if self.cwd != "/":
            self.navigate(self.cwd.rsplit("/", 1)[0] or "/")

    def _selected_paths(self):
        return [_join(self.cwd, n) for n in self.tree.selection()]

    # -- mutations ------------------------------------------------
    def _guard_rw(self):
        if self.conn and self.conn.readonly:
            messagebox.showwarning("vdi", "This image is open read-only.")
            return False
        return True

    def mkdir(self):
        if not self._guard_rw():
            return
        name = _prompt(self, "New folder", "Folder name:")
        if not name:
            return
        p = _join(self.cwd, name)
        self.worker.submit(f"mkdir {p}", lambda: self.conn.ops.mkdir(p, parents=True),
                           lambda _r: self.refresh())

    def rename(self):
        if not self._guard_rw():
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        new = _prompt(self, "Rename", "New name:", sel[0])
        if not new or new == sel[0]:
            return
        src, dst = _join(self.cwd, sel[0]), _join(self.cwd, new)
        self.worker.submit(f"rename {src}", lambda: self.conn.ops.rename(src, dst),
                           lambda _r: self.refresh())

    def delete(self):
        if not self._guard_rw():
            return
        paths = self._selected_paths()
        if not paths or not messagebox.askyesno("vdi", f"Delete {len(paths)} item(s)?"):
            return

        def work():
            for p in paths:
                st = self.conn.ops.stat(p)
                if st.type == "dir":
                    self.conn.ops.rmdir(p, recursive=True)
                else:
                    self.conn.ops.rm(p)

        self.worker.submit(f"deleting {len(paths)} item(s)", work, lambda _r: self.refresh())

    def import_path(self):
        if not self._guard_rw():
            return
        f = filedialog.askopenfilename(title="Import file")
        d = None
        if not f:
            d = filedialog.askdirectory(title="Import folder")
        src = f or d
        if not src:
            return
        dst = self.cwd

        def work():
            if os.path.isdir(src):
                self.conn.ops.upload_tree(src, _join(dst, os.path.basename(src)))
            else:
                self.conn.ops.write(_join(dst, os.path.basename(src)),
                                    open(src, "rb").read())

        self.worker.submit(f"importing {os.path.basename(src)}", work, lambda _r: self.refresh())

    def export_path(self):
        paths = self._selected_paths()
        if not paths:
            return
        outdir = filedialog.askdirectory(title="Export to")
        if not outdir:
            return

        def work():
            for p in paths:
                st = self.conn.ops.stat(p)
                base = p.rsplit("/", 1)[-1]
                if st.type == "dir":
                    self.conn.ops.download_tree(p, os.path.join(outdir, base))
                else:
                    with open(os.path.join(outdir, base), "wb") as fh:
                        fh.write(self.conn.ops.read(p))

        self.worker.submit(f"exporting {len(paths)} item(s)", work,
                           lambda _r: self.status.config(text=f"exported to {outdir}"))

    def view_file(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        p = _join(self.cwd, sel[0])
        self.worker.submit(f"reading {p}",
                           lambda: self.conn.ops.read(p),
                           lambda data: _Viewer(self, p, data, self._save_file))

    def _save_file(self, path, data: bytes):
        if not self._guard_rw():
            return
        self.worker.submit(f"writing {path}",
                           lambda: self.conn.ops.write(path, data),
                           lambda _r: self.refresh())

    # -- image-level dialogs -------------------------------------
    def create_dialog(self):
        _CreateDialog(self, self.worker)

    def convert_dialog(self):
        src = filedialog.askopenfilename(title="Source image")
        if not src:
            return
        dst = filedialog.asksaveasfilename(title="Output image",
                                           filetypes=[("vhdx", "*.vhdx"), ("vmdk", "*.vmdk"),
                                                      ("qcow2", "*.qcow2"), ("raw", "*.raw")])
        if not dst:
            return

        def work():
            from vdi.image import QemuImg
            from vdi.engine import get_engine
            QemuImg(engine=get_engine("auto")).convert(src, dst)

        self.worker.submit(f"converting → {os.path.basename(dst)}", work,
                           lambda _r: messagebox.showinfo("vdi", "Conversion done."))

    def serve_dialog(self):
        path = filedialog.askopenfilename(title="Image to serve")
        if not path:
            return
        _ServeDialog(self, path)

    def _quit(self):
        self.close_conn(silent=True)
        self.destroy()


# ----------------------------------------------------------------------
# small dialogs
# ----------------------------------------------------------------------
class _AskOpts(tk.Toplevel):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.title("Open options")
        self.ok = False
        self.transient(parent)
        self.grab_set()
        ttk.Label(self, text=os.path.basename(path)).grid(row=0, column=0, columnspan=2, padx=8, pady=6)
        ttk.Label(self, text="Engine").grid(row=1, column=0, sticky=tk.E, padx=6)
        self._eng = tk.StringVar(value="auto")
        ttk.Combobox(self, textvariable=self._eng, values=["auto", "wsl", "qemu", "local"],
                     width=12, state="readonly").grid(row=1, column=1, sticky=tk.W, pady=3)
        ttk.Label(self, text="Format").grid(row=2, column=0, sticky=tk.E, padx=6)
        self._fmt = tk.StringVar(value="auto")
        ttk.Combobox(self, textvariable=self._fmt, width=12, state="readonly",
                     values=["auto", "vmdk", "vhdx", "vhd", "qcow2", "raw"]).grid(
            row=2, column=1, sticky=tk.W, pady=3)
        ttk.Label(self, text="Partition").grid(row=3, column=0, sticky=tk.E, padx=6)
        self._part = tk.StringVar()
        ttk.Entry(self, textvariable=self._part, width=14).grid(row=3, column=1, sticky=tk.W)
        self._ro = tk.BooleanVar()
        ttk.Checkbutton(self, text="Read-only", variable=self._ro).grid(row=4, column=1, sticky=tk.W)
        b = ttk.Frame(self)
        b.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(b, text="Open", command=self._go).pack(side=tk.LEFT, padx=4)
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def _go(self):
        self.engine = self._eng.get()
        self.partition = self._part.get().strip()
        self.readonly = self._ro.get()
        self.fmt = self._fmt.get()
        self.ok = True
        self.destroy()


class _Viewer(tk.Toplevel):
    def __init__(self, parent, path, data: bytes, on_save):
        super().__init__(parent)
        self.title(path)
        self.geometry("640x460")
        self.path, self.on_save = path, on_save
        try:
            text = data.decode("utf-8")
            self.binary = False
        except UnicodeDecodeError:
            text = data.hex(" ")
            self.binary = True
        self.txt = scrolledtext.ScrolledText(self, wrap=tk.NONE, font=("Consolas", 10))
        self.txt.insert("1.0", text)
        self.txt.pack(fill=tk.BOTH, expand=True)
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text=f"{len(data)} bytes" + ("  (binary — read only)" if self.binary else "")
                  ).pack(side=tk.LEFT, padx=6)
        if not self.binary:
            ttk.Button(bar, text="Save", command=self._save).pack(side=tk.RIGHT, padx=4, pady=3)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side=tk.RIGHT)

    def _save(self):
        self.on_save(self.path, self.txt.get("1.0", "end-1c").encode("utf-8"))
        self.destroy()


class _CreateDialog(tk.Toplevel):
    def __init__(self, parent, worker):
        super().__init__(parent)
        self.title("Create image from folder")
        self.worker = worker
        g = lambda r, t: ttk.Label(self, text=t).grid(row=r, column=0, sticky=tk.E, padx=6, pady=3)
        self.folder = tk.StringVar(); self.out = tk.StringVar()
        self.fs = tk.StringVar(value="ext4"); self.size = tk.StringVar(value="1G")
        self.label = tk.StringVar()
        g(0, "Source folder")
        ttk.Entry(self, textvariable=self.folder, width=34).grid(row=0, column=1)
        ttk.Button(self, text="…", width=3,
                   command=lambda: self.folder.set(filedialog.askdirectory())).grid(row=0, column=2)
        g(1, "Output image")
        ttk.Entry(self, textvariable=self.out, width=34).grid(row=1, column=1)
        ttk.Button(self, text="…", width=3, command=self._pick_out).grid(row=1, column=2)
        g(2, "Filesystem")
        ttk.Combobox(self, textvariable=self.fs, width=10, state="readonly",
                     values=["fat16", "fat32", "exfat", "ext2", "ext3", "ext4"]).grid(row=2, column=1, sticky=tk.W)
        g(3, "Size"); ttk.Entry(self, textvariable=self.size, width=10).grid(row=3, column=1, sticky=tk.W)
        g(4, "Label"); ttk.Entry(self, textvariable=self.label, width=14).grid(row=4, column=1, sticky=tk.W)
        ttk.Button(self, text="Create", command=self._go).grid(row=5, column=1, pady=8)

    def _pick_out(self):
        self.out.set(filedialog.asksaveasfilename(
            filetypes=[("vmdk", "*.vmdk"), ("vhdx", "*.vhdx"), ("iso", "*.iso")]))

    def _go(self):
        folder, out = self.folder.get(), self.out.get()
        fs, size, label = self.fs.get(), self.size.get(), self.label.get()
        if not folder or not out:
            return
        ext = os.path.splitext(out)[1].lower()
        if ext not in (".iso", ".vmdk", ".vhdx", ".vhd", ".qcow2", ".raw", ".img"):
            out += ".vmdk"          # sensible default when the name has no extension
        self.destroy()

        def work():
            from vdi.engine import get_engine
            from vdi.image import fmt_from_path
            eng = get_engine("auto")
            if out.lower().endswith(".iso"):
                eng.build_iso(folder, out, volid=label)
            else:
                eng.build_from_folder(folder, out, fmt=fmt_from_path(out), fs=fs,
                                      size=size, label=label)

        self.worker.submit(f"creating {os.path.basename(out)} …", work,
                           lambda _r: messagebox.showinfo("vdi", f"Created {out}"))


class _ServeDialog(tk.Toplevel):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.title("Serve image")
        self.path = path
        self.name = tk.StringVar(value=os.path.splitext(os.path.basename(path))[0])
        self.ftp = tk.BooleanVar(); self.dav = tk.BooleanVar(); self.mcp = tk.BooleanVar()
        self.ro = tk.BooleanVar()
        ttk.Label(self, text=os.path.basename(path)).grid(row=0, column=0, columnspan=2, pady=5)
        ttk.Label(self, text="Session name").grid(row=1, column=0, sticky=tk.E, padx=6)
        ttk.Entry(self, textvariable=self.name).grid(row=1, column=1, sticky=tk.W)
        ttk.Checkbutton(self, text="FTP  (:2121)", variable=self.ftp).grid(row=2, column=1, sticky=tk.W)
        ttk.Checkbutton(self, text="WebDAV  (:8080)", variable=self.dav).grid(row=3, column=1, sticky=tk.W)
        ttk.Checkbutton(self, text="MCP  (:7333, writable)", variable=self.mcp).grid(row=4, column=1, sticky=tk.W)
        ttk.Checkbutton(self, text="Read-only", variable=self.ro).grid(row=5, column=1, sticky=tk.W)
        ttk.Button(self, text="Start (new process)", command=self._go).grid(row=6, column=1, pady=8)

    def _go(self):
        import subprocess
        import sys
        args = [sys.executable, "-m", "vdi", "serve", self.path, "--name", self.name.get()]
        if self.ro.get():
            args.append("--readonly")
        if self.ftp.get():
            args += ["--ftp", "127.0.0.1:2121"]
        if self.dav.get():
            args += ["--webdav", "127.0.0.1:8080"]
        if self.mcp.get():
            args += ["--mcp", "127.0.0.1:7333", "--mcp-writable"]
        subprocess.Popen(args, env={**os.environ, "PYTHONPATH": _srcpath()})
        messagebox.showinfo("vdi", "Server starting in a new process.\n"
                            "Use File ▸ Attach to session once it is ready.")
        self.destroy()


# ----------------------------------------------------------------------
def _prompt(parent, title, label, initial=""):
    import tkinter.simpledialog
    return tkinter.simpledialog.askstring(title, label, initialvalue=initial, parent=parent)


def _join(base, name):
    return normalize_inner((base.rstrip("/") + "/" + name) if base != "/" else "/" + name)


def _human(n):
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def _ts(t):
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t or 0)) if t else ""


def _srcpath():
    return os.pathsep.join(p for p in __import__("sys").path if p)


def main(argv=None) -> int:
    try:
        app = VdiGui()
    except tk.TclError as e:
        print(f"cannot start GUI ({e}). On Linux: install python3-tk.")
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
