"""流式下载：增量哈希、磁盘空间预检、重试与进度回调。"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from ..errors import ImageFetchError
from . import http
from .models import (
    Client,
    DownloadResult,
    Progress,
    RemoteTarget,
    _PermanentError,
)

CHUNK_SIZE = 1024 * 1024


def _free_space(directory: Path) -> int:
    stats = os.statvfs(directory)
    return stats.f_bavail * stats.f_frsize


def download(
    target: RemoteTarget,
    part_path: Path,
    *,
    algo: str = "sha256",
    expected: str | None = None,
    client: Client | None = None,
    progress: Progress | None = None,
    free_space: Callable[[Path], int] = _free_space,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """流式下载到 ``part_path``，同时增量计算哈希。

    校验失败会删除临时文件并重新尝试；全部候选失败时抛 ImageFetchError。
    正式镜像只有在调用方校验通过并原子重命名后才出现。
    """
    http_client, owned = http._use_client(client)
    try:
        failures: list[str] = []
        for url in target.urls:
            # 每次重试从头计数，避免进度条在重置后倒退
            if progress is not None:
                progress.update(0, None)
            for attempt in range(http.RETRIES):
                try:
                    return _download_once(
                        http_client,
                        url,
                        part_path,
                        algo=algo,
                        expected=expected,
                        progress=progress,
                        free_space=free_space,
                    )
                except _ChecksumMismatch as exc:
                    failures.append(str(exc))
                    _unlink(part_path)
                except _PermanentError as exc:
                    failures.append(f"{url} ({exc})")
                    break
                except (httpx.HTTPError, OSError) as exc:
                    failures.append(f"{url} ({http._describe(exc)})")
                    _unlink(part_path)
                except KeyboardInterrupt:
                    # Ctrl+C 取消时清掉半成品，正式镜像始终保持不变
                    _unlink(part_path)
                    raise
                if attempt + 1 < http.RETRIES:
                    sleep(http.BACKOFF[min(attempt, len(http.BACKOFF) - 1)])
        raise ImageFetchError(
            f"Failed to download {target.filename}: " + "; ".join(failures)
        )
    finally:
        if progress is not None:
            progress.close()
        if owned:
            http_client.close()


class _ChecksumMismatch(Exception):
    pass


def _download_once(
    client: Client,
    url: str,
    part_path: Path,
    *,
    algo: str,
    expected: str | None,
    progress: Progress | None,
    free_space: Callable[[Path], int],
) -> DownloadResult:
    _unlink(part_path)
    digest = hashlib.new(algo)
    written = 0
    with client.stream("GET", url) as response:
        http._check_response(response)
        total = _content_length(response)
        if total is None:
            # 部分源（如 AlmaLinux）用 chunked 传输，GET 无 Content-Length；
            # HEAD 通常仍返回准确大小，用于进度与磁盘空间预检。
            total = _remote_size(client, url)
        if total is not None and total > free_space(part_path.parent):
            raise ImageFetchError(
                f"Not enough free disk space for {part_path.name}: "
                f"need {total} bytes"
            )
        if progress is not None:
            progress.update(0, total)
        with open(part_path, "wb") as handle:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                data = bytes(chunk)
                handle.write(data)
                digest.update(data)
                written += len(data)
                if progress is not None:
                    progress.update(written, total)

    actual = digest.hexdigest()
    if expected is not None and actual != expected.lower():
        raise _ChecksumMismatch(
            f"{url}: checksum mismatch (expected {expected}, got {actual})"
        )
    return DownloadResult(checksum=actual, size=written)


def _content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _remote_size(client: Client, url: str) -> int | None:
    """用 HEAD 获取文件总大小；不支持或失败时返回 None（进度显示当前值）。"""
    try:
        response = client.head(url)
        http._check_response(response)
    except (httpx.HTTPError, _PermanentError):
        return None
    return _content_length(response)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
