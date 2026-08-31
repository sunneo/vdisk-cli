"""Client-side adapter: talk to a running ``vdi serve`` as if it were local ops."""
from __future__ import annotations

import base64

from vdi.fsops import DirEntry, DfInfo, GrepHit, StatInfo
from vdi.rpc import RpcClient


class RemoteOps:
    def __init__(self, client: RpcClient):
        self.c = client

    def fs_type(self) -> str:
        return self.c.call("ping").get("fs_type", "unknown")

    def df(self) -> DfInfo:
        return DfInfo(**self.c.call("fs.df"))

    def ls(self, path, *, long=False, recursive=False):
        rows = self.c.call("fs.ls", path=path, long=long, recursive=recursive)
        return [DirEntry(**r) for r in rows]

    def stat(self, path) -> StatInfo:
        return StatInfo(**self.c.call("fs.stat", path=path))

    def tree_size(self, path, *, apparent=False) -> int:
        return self.c.call("fs.size", path=path, apparent=apparent)["bytes"]

    def read(self, path, offset=0, length=None) -> bytes:
        r = self.c.call("fs.read", path=path, offset=offset, length=length)
        return base64.b64decode(r["data"])

    def write(self, path, data: bytes, offset=0, *, append=False) -> None:
        self.c.call("fs.write", path=path, data=base64.b64encode(data).decode(),
                    encoding="base64", offset=offset, append=append)

    def mkdir(self, path, *, parents=False):
        self.c.call("fs.mkdir", path=path, parents=parents)

    def rmdir(self, path, *, recursive=False):
        self.c.call("fs.rmdir", path=path, recursive=recursive)

    def rm(self, path):
        self.c.call("fs.rm", path=path)

    def rename(self, src, dst):
        self.c.call("fs.rename", src=src, dst=dst)

    def chmod(self, path, mode):
        self.c.call("fs.chmod", path=path, mode=mode)

    def chown(self, path, uid, gid):
        self.c.call("fs.chown", path=path, uid=uid, gid=gid)

    def upload_tree(self, local_path, dst):
        self.c.call("fs.copyIn", host_path=local_path, dst=dst)

    def download_tree(self, src, local_path):
        self.c.call("fs.copyOut", src=src, host_path=local_path)

    def grep(self, pattern, path="/", *, glob=None, ignore_case=False, max_results=1000):
        rows = self.c.call("fs.grep", pattern=pattern, path=path, glob=glob,
                           ignore_case=ignore_case, max_results=max_results)
        return [GrepHit(**r) for r in rows]

    def close(self):
        pass
