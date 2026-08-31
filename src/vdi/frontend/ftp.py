"""Minimal RFC 959 FTP server (stdlib only) backed by an FsView.

Supports the subset every FTP client needs for browse + read + write:
USER/PASS, PWD/CWD/CDUP, TYPE, PASV/EPSV, LIST/NLST, RETR, STOR/APPE,
DELE, MKD, RMD, RNFR/RNTO, SIZE, MDTM, SYST, FEAT, NOOP, QUIT.

Active mode (PORT) is intentionally not implemented -- passive only.
Auth: any username; password must equal the session token (or be anything
when ``token`` is None).
"""
from __future__ import annotations

import socket
import socketserver
import threading
import time

from vdi.errors import VdiError


class _FtpHandler(socketserver.StreamRequestHandler):
    view = None          # set by the server subclass
    token = None
    banner = "vdi FTP"

    def _send(self, code, text=""):
        self.wfile.write(f"{code} {text}\r\n".encode())

    def handle(self):
        self.cwd = "/"
        self.authed = False
        self._user = None
        self._pasv = None
        self._rest = 0
        self._rnfr = None
        self._send(220, self.banner)
        try:
            for raw in self.rfile:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper()
                fn = getattr(self, "ftp_" + cmd, None)
                if fn is None:
                    self._send(502, f"{cmd} not implemented")
                    continue
                if cmd not in ("USER", "PASS", "QUIT", "FEAT", "SYST", "NOOP") and not self.authed:
                    self._send(530, "Log in first")
                    continue
                try:
                    fn(arg)
                except VdiError as e:
                    self._send(550, str(e))
                except BrokenPipeError:
                    break
                except Exception as e:  # pragma: no cover
                    self._send(550, f"error: {e}")
                if cmd == "QUIT":
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass

    # -- auth ----------------------------------------------------
    def ftp_USER(self, arg):
        self._user = arg or "anonymous"
        self._send(331, "Password required")

    def ftp_PASS(self, arg):
        if self.token is not None and arg != self.token:
            self._send(530, "Bad token (use the session token as the password)")
            return
        self.authed = True
        self._send(230, f"Logged in as {self._user}")

    def ftp_QUIT(self, arg):
        self._send(221, "Bye")

    def ftp_NOOP(self, arg):
        self._send(200, "OK")

    def ftp_SYST(self, arg):
        self._send(215, "UNIX Type: L8")

    def ftp_FEAT(self, arg):
        self.wfile.write(b"211-Features:\r\n PASV\r\n EPSV\r\n SIZE\r\n MDTM\r\n UTF8\r\n REST STREAM\r\n211 End\r\n")

    def ftp_TYPE(self, arg):
        self._send(200, "Type set")

    def ftp_OPTS(self, arg):
        self._send(200, "OK")

    def ftp_PWD(self, arg):
        self._send(257, f'"{self.cwd}" is the current directory')

    # -- navigation --------------------------------------------
    def _resolve(self, arg):
        if not arg:
            return self.cwd
        if arg.startswith("/"):
            p = arg
        else:
            p = (self.cwd.rstrip("/") + "/" + arg)
        return self.view.real(p)

    def ftp_CWD(self, arg):
        target = self._resolve(arg)
        if not self.view.is_dir(target):
            self._send(550, "Not a directory")
            return
        self.cwd = target
        self._send(250, f'CWD to {target}')

    def ftp_CDUP(self, arg):
        self.ftp_CWD("..")

    # -- data channel -----------------------------------------
    def _open_pasv(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind((self.server.server_address[0], 0))
        srv.listen(1)
        srv.settimeout(30)
        return srv

    def ftp_PASV(self, arg):
        srv = self._open_pasv()
        self._pasv = srv
        host = self.request.getsockname()[0]
        port = srv.getsockname()[1]
        h = host.split(".")
        self._send(227, f"Entering Passive Mode ({h[0]},{h[1]},{h[2]},{h[3]},{port // 256},{port % 256})")

    def ftp_EPSV(self, arg):
        srv = self._open_pasv()
        self._pasv = srv
        self._send(229, f"Entering Extended Passive Mode (|||{srv.getsockname()[1]}|)")

    def _accept_data(self):
        if not self._pasv:
            self._send(425, "Use PASV first")
            return None
        conn, _ = self._pasv.accept()
        self._pasv.close()
        self._pasv = None
        return conn

    def ftp_REST(self, arg):
        self._rest = int(arg or 0)
        self._send(350, f"Restarting at {self._rest}")

    # -- listings ---------------------------------------------
    def _list_lines(self, path, names_only):
        entries = self.view.listdir(path)
        out = []
        for e in entries:
            if names_only:
                out.append(e.name)
            else:
                perms = "d" if e.type == "dir" else "-"
                mode = _mode_str(e.mode)
                ts = time.strftime("%b %d %H:%M", time.localtime(e.mtime or 0))
                out.append(f"{perms}{mode} 1 owner group {e.size:>12} {ts} {e.name}")
        return "\r\n".join(out) + ("\r\n" if out else "")

    def ftp_LIST(self, arg):
        self._do_list(arg, names_only=False)

    def ftp_NLST(self, arg):
        self._do_list(arg, names_only=True)

    def _do_list(self, arg, names_only):
        target = self._resolve(arg) if arg and not arg.startswith("-") else self.cwd
        conn = self._accept_data()
        if conn is None:
            return
        self._send(150, "Here comes the listing")
        try:
            conn.sendall(self._list_lines(target, names_only).encode())
        finally:
            conn.close()
        self._send(226, "Directory send OK")

    # -- transfers -------------------------------------------
    def ftp_RETR(self, arg):
        target = self._resolve(arg)
        conn = self._accept_data()
        if conn is None:
            return
        self._send(150, "Opening data connection")
        try:
            data = self.view.read(target, self._rest or 0, None)
            conn.sendall(data)
        finally:
            conn.close()
            self._rest = 0
        self._send(226, "Transfer complete")

    def _store(self, arg, append):
        target = self._resolve(arg)
        conn = self._accept_data()
        if conn is None:
            return
        self._send(150, "Ready for data")
        buf = bytearray()
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            conn.close()
        self.view.write(target, bytes(buf), append=append)
        self._send(226, "Transfer complete")

    def ftp_STOR(self, arg):
        self._store(arg, append=False)

    def ftp_APPE(self, arg):
        self._store(arg, append=True)

    # -- mutations -----------------------------------------
    def ftp_DELE(self, arg):
        self.view.remove(self._resolve(arg))
        self._send(250, "Deleted")

    def ftp_MKD(self, arg):
        self.view.mkdir(self._resolve(arg))
        self._send(257, f'"{self._resolve(arg)}" created')

    def ftp_RMD(self, arg):
        self.view.rmdir(self._resolve(arg))
        self._send(250, "Removed")

    def ftp_RNFR(self, arg):
        self._rnfr = self._resolve(arg)
        self._send(350, "Ready for RNTO")

    def ftp_RNTO(self, arg):
        if not self._rnfr:
            self._send(503, "RNFR first")
            return
        self.view.rename(self._rnfr, self._resolve(arg))
        self._rnfr = None
        self._send(250, "Renamed")

    def ftp_SIZE(self, arg):
        self._send(213, str(self.view.stat(self._resolve(arg)).size))

    def ftp_MDTM(self, arg):
        mt = self.view.stat(self._resolve(arg)).mtime
        self._send(213, time.strftime("%Y%m%d%H%M%S", time.localtime(mt or 0)))


def _mode_str(octal_mode: str) -> str:
    try:
        bits = int(octal_mode, 8)
    except ValueError:
        bits = 0o644
    out = ""
    for shift in (6, 3, 0):
        v = (bits >> shift) & 7
        out += ("r" if v & 4 else "-") + ("w" if v & 2 else "-") + ("x" if v & 1 else "-")
    return out


class _ThreadingFtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FtpFrontend:
    def __init__(self, view, host="127.0.0.1", port=2121, token=None):
        handler = type("H", (_FtpHandler,), {"view": view, "token": token})
        self.server = _ThreadingFtpServer((host, port), handler)
        self.host, self.port = self.server.server_address

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self):
        self.server.shutdown()
