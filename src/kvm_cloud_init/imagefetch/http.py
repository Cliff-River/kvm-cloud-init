"""基础 HTTP：统一客户端、状态码分类与重试。

HTTP 统一走 httpx，不调用 curl/wget 子进程。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from .. import __version__
from ..errors import ImageFetchError
from .models import Client, _PermanentError

TIMEOUT = 30
RETRIES = 3
BACKOFF = (2.0, 4.0)
USER_AGENT = f"kvmci/{__version__}"


def new_client() -> Client:
    """创建统一配置的 HTTP 客户端；调用方负责 close()。"""
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _use_client(client: Client | None) -> tuple[Client, bool]:
    """返回 (客户端, 是否由本模块创建)，便于统一关闭临时客户端。"""
    if client is not None:
        return client, False
    return new_client(), True


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
    return str(exc) or exc.__class__.__name__


def _check_response(response: httpx.Response) -> None:
    """4xx（除 408/429）视为不可重试，其余状态错误交由重试逻辑处理。"""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if 400 <= code < 500 and code not in (408, 429):
            raise _PermanentError(_describe(exc)) from exc
        raise


def _attempt(
    url: str,
    action: Callable[[], object],
    *,
    sleep: Callable[[float], None],
) -> object:
    """对单个 URL 执行最多 RETRIES 次，网络类错误指数退避重试。"""
    last: BaseException | None = None
    for attempt in range(RETRIES):
        try:
            return action()
        except _PermanentError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            if attempt + 1 < RETRIES:
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    assert last is not None
    raise _PermanentError(_describe(last)) from last


def fetch_text(
    urls: tuple[str, ...],
    *,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """按顺序尝试候选地址，返回第一份可读文本；全部失败抛 ImageFetchError。"""
    http, owned = _use_client(client)
    try:
        failures: list[str] = []
        for url in urls:
            try:
                return _fetch_text_one(url, http, sleep=sleep)
            except _PermanentError as exc:
                failures.append(f"{url} ({exc})")
        raise ImageFetchError(
            "Failed to fetch " + "; ".join(failures) if failures else "Failed to fetch"
        )
    finally:
        if owned:
            http.close()


def _fetch_text_one(
    url: str,
    http: Client,
    *,
    sleep: Callable[[float], None],
) -> str:
    def read() -> str:
        response = http.get(url)
        _check_response(response)
        return response.text

    return str(_attempt(url, read, sleep=sleep))
