"""宿主机与实例之间的文件搬运：``cp`` 与 ``sync``。

- ``paths``：``<实例名>:<路径>`` 解析与远端连接准备（两者共用）
- ``cp``：文件/目录拷贝（scp 语义）
- ``sync``：目录镜像（rclone sync 语义）
"""

from __future__ import annotations

from .cp import copy_file
from .paths import RemotePath, parse_remote
from .sync import sync_paths

__all__ = [
    "RemotePath",
    "copy_file",
    "parse_remote",
    "sync_paths",
]
