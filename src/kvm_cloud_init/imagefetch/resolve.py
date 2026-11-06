"""镜像目标解析与校验清单。

``url`` 源直接使用候选直链；``index`` 源拉取目录页、按正则筛选并挑最新。
校验清单同时兼容 coreutils（``<hash>  <file>``）与 BSD
（``SHA256 (<file>) = <hash>``）格式。
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..errors import ImageFetchError
from . import http, index
from .models import Client, RemoteTarget

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from ..config import ImageSource

_HASH_LENGTHS = {"sha256": 64, "sha512": 128}
_COREUTILS = re.compile(r"^([0-9a-fA-F]+)\s+\*?(.+?)\s*$")
_BSD = re.compile(r"^[A-Za-z0-9]+ \((.+)\) = ([0-9a-fA-F]+)\s*$")


def resolve_target(
    source: ImageSource,
    *,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RemoteTarget:
    """把配置中的镜像源解析为可下载目标。

    - ``url`` 种：直接使用配置的候选直链；
    - ``index`` 种：拉取目录页并按 ``match`` 选最新，同时定位校验清单。
    """
    if source.kind == "url":
        filename = index.basename(source.urls[0])
        if not filename:
            raise ImageFetchError(
                f"Image source {source.id} has an invalid URL: {source.urls[0]}"
            )
        return RemoteTarget(
            filename=filename,
            urls=tuple(source.urls),
            checksum_url=source.checksum_url or None,
            version=index.version_of(filename),
            algo=source.checksum_algo,
        )

    http_client, owned = http._use_client(client)
    try:
        failures: list[str] = []
        for page in source.pages:
            try:
                html = http.fetch_text((page,), client=http_client, sleep=sleep)
            except ImageFetchError as exc:
                failures.append(f"{page} ({exc})")
                continue
            candidates = index.parse_index(html, page, source.match or "")
            if not candidates:
                failures.append(f"{page} (no file matched {source.match!r})")
                continue
            chosen = index.pick_latest(
                candidates, source.order_by, client=http_client, sleep=sleep
            )
            checksum_name = source.checksum_file or _match_checksum_name(
                html, source.checksum_match
            )
            checksum_url = (
                urllib.parse.urljoin(page, checksum_name) if checksum_name else None
            )
            return RemoteTarget(
                filename=chosen.filename,
                urls=(chosen.url,),
                checksum_url=checksum_url,
                version=chosen.version,
                algo=source.checksum_algo,
            )
        raise ImageFetchError(
            f"No usable image found for {source.id}: " + "; ".join(failures)
        )
    finally:
        if owned:
            http_client.close()


def _match_checksum_name(html: str, pattern: str) -> str | None:
    if not pattern:
        return None
    regex = re.compile(pattern)
    for href in index.extract_hrefs(html):
        name = index.basename(href)
        if name and regex.search(name):
            return href
    return None


def parse_checksum(text: str, filename: str, algo: str) -> str:
    """从校验清单中取出指定文件的哈希；支持 coreutils 与 BSD 两种格式。"""
    expected_length = _HASH_LENGTHS.get(algo)
    if expected_length is None:
        raise ImageFetchError(f"Unsupported checksum algorithm: {algo}")

    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "Hash:")):
            continue
        digest: str | None = None
        coreutils = _COREUTILS.match(stripped)
        if coreutils and coreutils.group(2) == filename:
            digest = coreutils.group(1)
        else:
            bsd = _BSD.match(stripped)
            if bsd and bsd.group(1) == filename:
                digest = bsd.group(2)
        if digest is None:
            continue
        if len(digest) != expected_length:
            raise ImageFetchError(
                f"Checksum for {filename} has unexpected length "
                f"({len(digest)}, expected {expected_length} for {algo})"
            )
        found.append(digest.lower())

    if not found:
        raise ImageFetchError(
            f"No {algo} checksum found for {filename} in the checksum list"
        )
    if len(found) > 1:
        raise ImageFetchError(
            f"Ambiguous checksum list: {len(found)} entries for {filename}"
        )
    return found[0]


def fetch_checksum(
    target: RemoteTarget,
    *,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """拉取并解析目标文件的校验和；没有清单地址时返回 None。"""
    if not target.checksum_url:
        return None
    text = http.fetch_text((target.checksum_url,), client=client, sleep=sleep)
    return parse_checksum(text, target.filename, target.algo)
