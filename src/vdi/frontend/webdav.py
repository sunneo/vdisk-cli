"""Minimal WebDAV (class 1/2) server over http.server -- stdlib only.

Handles OPTIONS, HEAD, GET, PUT, DELETE, MKCOL, MOVE, COPY, PROPFIND (depth 0/1).
Enough for Windows Explorer "Map network drive", macOS Finder, rclone, and
`davfs2`. No locking (LOCK/UNLOCK return 200 with a fake token so Office/Explorer
are happy). Optional Basic auth: password == session token.
"""
from __future__ import annotations

import base64
import html
import http.server
import threading
import time
import urllib.parse
from xml.sax.saxutils import escape

from vdi.errors import NotFound, VdiError


class _Handler(http.server.BaseHTTPRequestHandler):
    view = None
    token = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # -- helpers ----------------------------------------------
    def _auth_ok(self) -> bool:
        if self.token is None:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
                return pw == self.token
            except Exception:
                return False
        return False

    def _need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="vdi"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _path(self) -> str:
        p = urllib.parse.unquote(self.path.split("?", 1)[0])
        return p or "/"

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def _reply(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("DAV", "1, 2")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _guard(self) -> bool:
        if not self._auth_ok():
            self._need_auth()
            return False
        return True

    def _run(self, fn):
        if not self._guard():
            return
        try:
            fn()
        except NotFound:
            self._reply(404, "not found", "text/plain")
        except VdiError as e:
            self._reply(409, str(e), "text/plain")
        except Exception as e:  # pragma: no cover
            self._reply(500, f"{e}", "text/plain")

    # -- verbs ------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("DAV", "1, 2")
        self.send_header("Allow", "OPTIONS,HEAD,GET,PUT,DELETE,MKCOL,MOVE,COPY,PROPFIND,LOCK,UNLOCK")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._run(self._get)

    def do_GET(self):
        self._run(self._get)

    def _get(self):
        p = self._path()
        st = self.view.stat(p)
        if st.type == "dir":
            self._reply(200, _dir_html(p, self.view.listdir(p)), "text/html; charset=utf-8")
            return
        data = self.view.read(p)
        self._reply(200, data, _ctype(p), extra={
            "Last-Modified": _httpdate(st.mtime),
            "Accept-Ranges": "none",
        })

    def do_PUT(self):
        def _do():
            self.view.write(self._path(), self._body())
            self._reply(201, b"", "text/plain")
        self._run(_do)

    def do_DELETE(self):
        def _do():
            p = self._path()
            if self.view.is_dir(p):
                self.view.rmdir(p)
            else:
                self.view.remove(p)
            self._reply(204)
        self._run(_do)

    def do_MKCOL(self):
        def _do():
            self.view.mkdir(self._path())
            self._reply(201)
        self._run(_do)

    def do_MOVE(self):
        def _do():
            dst = self._dest()
            self.view.rename(self._path(), dst)
            self._reply(201)
        self._run(_do)

    def do_COPY(self):
        def _do():
            src, dst = self._path(), self._dest()
            data = self.view.read(src)
            self.view.write(dst, data)
            self._reply(201)
        self._run(_do)

    def _dest(self) -> str:
        d = self.headers.get("Destination", "")
        d = urllib.parse.urlparse(d).path
        return urllib.parse.unquote(d) or "/"

    def do_LOCK(self):
        if not self._guard():
            return
        tok = "opaquelocktoken:vdi-%d" % int(time.time() * 1000)
        body = (f'<?xml version="1.0"?><D:prop xmlns:D="DAV:"><D:lockdiscovery>'
                f'<D:activelock><D:locktoken><D:href>{tok}</D:href></D:locktoken>'
                f'</D:activelock></D:lockdiscovery></D:prop>')
        self._reply(200, body, "application/xml", extra={"Lock-Token": f"<{tok}>"})

    def do_UNLOCK(self):
        if not self._guard():
            return
        self._reply(204)

    def do_PROPFIND(self):
        if not self._guard():
            return
        p = self._path()
        depth = self.headers.get("Depth", "1")
        try:
            st = self.view.stat(p)
        except NotFound:
            self._reply(404, "not found", "text/plain")
            return
        items = [(p, st)]
        if st.type == "dir" and depth != "0":
            for e in self.view.listdir(p):
                child = (p.rstrip("/") + "/" + e.name)
                items.append((child, e))
        xml = ['<?xml version="1.0" encoding="utf-8"?>',
               '<D:multistatus xmlns:D="DAV:">']
        for href, meta in items:
            is_dir = getattr(meta, "type", "file") == "dir"
            size = getattr(meta, "size", 0)
            mtime = getattr(meta, "mtime", 0)
            xml.append(
                "<D:response><D:href>{h}</D:href><D:propstat><D:prop>"
                "<D:resourcetype>{rt}</D:resourcetype>"
                "<D:getcontentlength>{sz}</D:getcontentlength>"
                "<D:getlastmodified>{lm}</D:getlastmodified>"
                "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>".format(
                    h=escape(urllib.parse.quote(href)),
                    rt="<D:collection/>" if is_dir else "",
                    sz=size, lm=_httpdate(mtime)))
        xml.append("</D:multistatus>")
        self._reply(207, "\n".join(xml), 'application/xml; charset="utf-8"')


def _ctype(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _httpdate(ts) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts or 0))


def _dir_html(path, entries) -> str:
    rows = "".join(
        f'<li><a href="{urllib.parse.quote((path.rstrip("/") + "/" + e.name))}">'
        f'{html.escape(e.name)}{"/" if e.type == "dir" else ""}</a> '
        f'({e.size} bytes)</li>' for e in entries)
    return f"<!doctype html><title>{html.escape(path)}</title><h1>{html.escape(path)}</h1><ul>{rows}</ul>"


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class WebdavFrontend:
    def __init__(self, view, host="127.0.0.1", port=8080, token=None):
        handler = type("H", (_Handler,), {"view": view, "token": token})
        self.server = _Server((host, port), handler)
        self.host, self.port = self.server.server_address

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self):
        self.server.shutdown()
