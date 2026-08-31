import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from vdi.wire import Codec, pick


CASES = [
    {"jsonrpc": "2.0", "id": 1, "method": "fs.stat", "params": {"token": "t", "path": "/a/b"}},
    {"jsonrpc": "2.0", "id": 2, "result": {"type": "file", "size": 283, "mode": "0644",
                                           "uid": 0, "gid": 0, "nlink": 1}},
    {"jsonrpc": "2.0", "id": 3, "result": [
        {"name": "a.txt", "type": "file", "size": 5, "mtime": 111, "mode": "0644"},
        {"name": "dir", "type": "dir", "size": 4096, "mtime": 222, "mode": "0755"}]},
    {"jsonrpc": "2.0", "id": 4, "result": []},
    {"jsonrpc": "2.0", "id": 5, "error": {"code": -32001, "message": "path does not exist"}},
    {"jsonrpc": "2.0", "id": 6, "result": {"text": "he said \"hi\", a,b|c", "truncated": False}},
    {"jsonrpc": "2.0", "id": 7, "result": {"length": 0}},
]


@pytest.mark.parametrize("name", ["json", "toon"])
@pytest.mark.parametrize("obj", CASES)
def test_roundtrip(name, obj):
    c = Codec(name)
    assert c.decode(c.encode(obj)) == obj


def test_toon_is_smaller_for_listings():
    listing = {"jsonrpc": "2.0", "id": 9, "result": [
        {"name": f"file{i}.txt", "type": "file", "size": i * 10, "mtime": 1700000000 + i,
         "mode": "0644"} for i in range(30)]}
    j = Codec("json").encode(listing)
    t = Codec("toon").encode(listing)
    assert Codec("toon").decode(t) == listing
    assert len(t) < len(j) * 0.7


def test_negotiation():
    assert pick(["toon", "json"]) == "toon"
    assert pick(["json"]) == "json"
    assert pick(["banana"]) == "json"
    assert pick([]) == "json"
