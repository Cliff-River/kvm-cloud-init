"""镜像获取的数据模型与协议。

不依赖 config / provision / hv，只做类型定义，便于各子模块共享。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

Client = httpx.Client


class Progress(Protocol):
    """下载进度接收器；由调用方实现并在下载结束后 ``close``。"""

    def update(self, done: int, total: int | None) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RemoteImage:
    """目录页中的一个候选镜像文件。"""

    filename: str
    url: str
    version: str


@dataclass(frozen=True)
class RemoteTarget:
    """解析后的下载目标：真实文件名、候选下载地址与校验清单地址。"""

    filename: str
    urls: tuple[str, ...]
    checksum_url: str | None
    version: str
    algo: str = "sha256"


@dataclass(frozen=True)
class DownloadResult:
    checksum: str
    size: int


class _PermanentError(Exception):
    """不可重试的失败（如 404），直接切换到下一个候选地址。"""
