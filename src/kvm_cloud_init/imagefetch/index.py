"""目录页解析：提取链接、按正则筛选候选并按文件名/时间挑最新。"""

from __future__ import annotations

import re
import sys
import time
import urllib.parse
from collections.abc import Callable
from email.utils import parsedate_to_datetime

from parsel import Selector

from ..errors import ImageFetchError
from . import http
from .archive import ARCHIVE_SUFFIXES
from .models import Client, RemoteImage, _PermanentError

_EXTENSIONS = ARCHIVE_SUFFIXES + (".qcow2", ".img", ".raw")


def extract_hrefs(html: str) -> list[str]:
    """用 parsel 的 CSS 选择器提取全部 ``<a href>``，不做正则式 HTML 解析。"""
    return Selector(text=html).css("a::attr(href)").getall()


def basename(href: str) -> str:
    """从 href 取出文件名；忽略 ../ 与排序参数等非文件链接。"""
    path = urllib.parse.urlsplit(href).path
    return path.rstrip("/").rsplit("/", 1)[-1]


def parse_index(html: str, page_url: str, pattern: str) -> list[RemoteImage]:
    """在目录页 HTML 中筛选出匹配正则的候选文件（按文件名排序）。"""
    regex = re.compile(pattern)
    seen: dict[str, RemoteImage] = {}
    for href in extract_hrefs(html):
        name = basename(href)
        if not name or name in seen or not regex.search(name):
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        seen[name] = RemoteImage(
            filename=name, url=absolute, version=version_of(name)
        )
    return sorted(seen.values(), key=lambda item: item.filename)


def version_of(filename: str) -> str:
    """去掉已知镜像后缀作为展示用版本号。"""
    lowered = filename.lower()
    for suffix in _EXTENSIONS:
        if lowered.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def pick_latest(
    candidates: list[RemoteImage],
    order_by: str = "name",
    *,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RemoteImage:
    """挑选最新候选；``mtime`` 依赖 HEAD 的 Last-Modified，失败时回退文件名。"""
    if not candidates:
        raise ImageFetchError("Empty image candidate list")
    if order_by == "mtime":
        http_client, owned = http._use_client(client)
        try:
            stamped = [
                (mtime_of(item.url, client=http_client, sleep=sleep), item)
                for item in candidates
            ]
        finally:
            if owned:
                http_client.close()
        if any(stamp is not None for stamp, _ in stamped):
            return max(
                (pair for pair in stamped if pair[0] is not None),
                key=lambda pair: pair[0],
            )[1]
        print(
            "Warning: Last-Modified unavailable; falling back to name ordering.",
            file=sys.stderr,
        )
    return max(candidates, key=lambda item: item.filename)


def mtime_of(
    url: str,
    *,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> float | None:
    """HEAD 请求的 Last-Modified 时间戳；不支持或失败时返回 None。"""
    http_client, owned = http._use_client(client)
    try:

        def head() -> float | None:
            response = http_client.head(url)
            http._check_response(response)
            value = response.headers.get("Last-Modified")
            if not value:
                return None
            try:
                return parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError):
                return None

        try:
            result = http._attempt(url, head, sleep=sleep)
        except _PermanentError:
            return None
        return result if isinstance(result, float) else None
    finally:
        if owned:
            http_client.close()
