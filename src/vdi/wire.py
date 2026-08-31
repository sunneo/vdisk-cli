"""Wire codecs + capability negotiation for the vdi RPC channel.

When a client connects it sends a ``hello`` line advertising the codecs it
supports; the server replies picking one. Everything after that frame uses the
chosen codec. Bulk file bytes never go through a codec -- they ride the raw
``"raw": N`` trailer (see rpc.py).

Codecs:
    json  -- always available, the baseline
    toon  -- Token-Oriented Object Notation: for the value shapes vdi actually
             sends (scalars, flat dicts, and lists of uniform flat dicts such as
             directory listings / grep hits) it is ~40-60% smaller than JSON
             because uniform lists become one header + bare CSV rows.

Negotiation frame (JSON, always):
    ->  {"hello": {"v": 1, "codecs": ["toon", "json"]}}
    <-  {"hello": {"v": 1, "codec": "toon"}}
"""
from __future__ import annotations

import json

PROTOCOL_VERSION = 1
DEFAULT_ORDER = ("toon", "json")


# ---------------------------------------------------------------- JSON
def _json_encode(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def _json_decode(data: bytes):
    return json.loads(data.decode())


# ---------------------------------------------------------------- TOON
# One-line, fully bracket-balanced encoding for vdi's RPC value shapes:
#
#   scalar := int | float | true | false | null | ~string~
#             (strings are always ~-delimited so "2.0" stays a string, and no
#              value ever needs to be guessed)
#   dict   := ( k=v ; k=v ; ... )
#   list   := [ v ; v ; ... ]                       list of scalars/dicts
#   table  := < f1 , f2 ; r1c1 , r1c2 ; r2c1 , r2c2 >   list of uniform dicts
#   empty list := []      empty dict := ()
#
# Nesting is supported (dicts/lists inside lists) but vdi only ever needs one
# level plus the tabular list.

_ESC = {"~": r"\~", "\\": r"\\", "\n": r"\n"}
_UNESC = {v: k for k, v in _ESC.items()}


def _q(s: str) -> str:
    for a, b in _ESC.items():
        s = s.replace(a, b)
    return f"~{s}~"


def _uq(tok: str) -> str:
    s = tok[1:-1]
    for b, a in _UNESC.items():
        s = s.replace(b, a)
    return s


def _scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return _q(str(v))


def _unscalar(tok: str):
    tok = tok.strip()
    if tok.startswith("~") and tok.endswith("~"):
        return _uq(tok)
    if tok == "null":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    try:
        return int(tok)
    except ValueError:
        return float(tok)


def _split(s: str, sep: str) -> list[str]:
    out, buf, depth, inq = [], [], 0, False
    i = 0
    while i < len(s):
        c = s[i]
        if inq:
            buf.append(c)
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i + 1]); i += 2; continue
            if c == "~":
                inq = False
        elif c == "~":
            inq = True; buf.append(c)
        elif c in "([<":
            depth += 1; buf.append(c)
        elif c in ")]>":
            depth -= 1; buf.append(c)
        elif c == sep and depth == 0:
            out.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def _toon_encode(obj) -> bytes:
    return _enc(obj).encode()


def _enc(v) -> str:
    if isinstance(v, dict):
        if not v:
            return "()"
        return "(" + ";".join(f"{_q(str(k))}={_enc(x)}" for k, x in v.items()) + ")"
    if isinstance(v, (list, tuple)):
        v = list(v)
        if not v:
            return "[]"
        if all(isinstance(x, dict) and x for x in v):
            fields = list(v[0].keys())
            if all(list(x.keys()) == fields for x in v) and \
               all(not isinstance(x[f], (dict, list)) for x in v for f in fields):
                head = ",".join(_q(str(f)) for f in fields)
                rows = ";".join(",".join(_scalar(x[f]) for f in fields) for x in v)
                return f"<{head};{rows}>"
        return "[" + ";".join(_enc(x) for x in v) + "]"
    return _scalar(v)


def _toon_decode(data: bytes):
    return _dec(data.decode().strip())


def _dec(s: str):
    s = s.strip()
    if s == "()":
        return {}
    if s == "[]":
        return []
    if s[:1] == "(" and s[-1:] == ")":
        d = {}
        for part in _split(s[1:-1], ";"):
            k, _, val = part.partition("=")
            d[_unscalar(k.strip())] = _dec(val)
        return d
    if s[:1] == "<" and s[-1:] == ">":
        parts = _split(s[1:-1], ";")
        fields = [_unscalar(f) for f in _split(parts[0], ",")]
        return [dict(zip(fields, (_unscalar(x) for x in _split(row, ","))))
                for row in parts[1:]]
    if s[:1] == "[" and s[-1:] == "]":
        return [_dec(x) for x in _split(s[1:-1], ";")]
    return _unscalar(s)


# ---------------------------------------------------------------- registry
CODECS = {
    "json": (_json_encode, _json_decode),
    "toon": (_toon_encode, _toon_decode),
}


def pick(client_codecs: list[str]) -> str:
    for c in DEFAULT_ORDER:
        if c in client_codecs and c in CODECS:
            return c
    return "json"


class Codec:
    def __init__(self, name: str):
        self.name = name
        self.encode, self.decode = CODECS[name]
