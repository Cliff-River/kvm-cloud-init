"""云镜像下载的纯能力层：目录页解析、校验和、流式下载与本地状态。

本模块不依赖 config / provision / hv，只做 I/O 与解析，便于单元测试时注入
假的 opener，全程不触网。

设计要点：

- 只使用标准库 ``urllib``，不引入第三方 HTTP 依赖，也不调用 curl/wget 子进程；
- 下载写入 ``.<file>.part`` 临时文件，校验通过后由调用方原子重命名；
- 校验清单同时支持 coreutils（``<hash>  <file>``）与 BSD
  （``SHA256 (<file>) = <hash>``）两种格式，以及 Fedora 的 PGP 包裹清单。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self

from . import __version__
from .errors import ImageFetchError

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from .config import ImageSource

STATE_FILENAME = ".state.json"
CHUNK_SIZE = 1024 * 1024
TIMEOUT = 30
RETRIES = 3
BACKOFF = (2.0, 4.0)
USER_AGENT = f"kvmci/{__version__}"

_HASH_LENGTHS = {"sha256": 64, "sha512": 128}
_EXTENSIONS = (".qcow2", ".img", ".raw")
_COREUTILS = re.compile(r"^([0-9a-fA-F]+)\s+\*?(.+?)\s*$")
_BSD = re.compile(r"^[A-Za-z0-9]+ \((.+)\) = ([0-9a-fA-F]+)\s*$")


class _Response(Protocol):
    """urllib 响应的最小接口，便于测试注入假对象。"""

    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...


Opener = Callable[..., Iterator[_Response]]
Progress = Callable[[int, int | None], None]


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


# --------------------------------------------------------------------------- #
# 基础 HTTP
# --------------------------------------------------------------------------- #


def _request(url: str, *, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def _default_opener(url: str, *, method: str = "GET", timeout: int = TIMEOUT) -> _Response:
    return urllib.request.urlopen(_request(url, method=method), timeout=timeout)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__


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
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            if attempt + 1 < RETRIES:
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise _PermanentError(_describe(last)) from last


def fetch_text(
    urls: tuple[str, ...],
    *,
    opener: Opener = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """按顺序尝试候选地址，返回第一份可读文本；全部失败抛 ImageFetchError。"""
    failures: list[str] = []
    for url in urls:
        try:
            return _fetch_text_one(url, opener=opener, sleep=sleep)
        except _PermanentError as exc:
            failures.append(f"{url} ({exc})")
    raise ImageFetchError(
        "Failed to fetch " + "; ".join(failures) if failures else "Failed to fetch"
    )


def _fetch_text_one(
    url: str,
    *,
    opener: Opener,
    sleep: Callable[[float], None],
) -> str:
    def read() -> str:
        with opener(url) as response:
            raw = response.read()
        return bytes(raw).decode("utf-8", errors="replace")

    return str(_attempt(url, read, sleep=sleep))


# --------------------------------------------------------------------------- #
# 目录页解析
# --------------------------------------------------------------------------- #


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def extract_hrefs(html: str) -> list[str]:
    """提取全部 <a href>，不做任何正则式 HTML 解析。"""
    parser = _AnchorParser()
    parser.feed(html)
    return parser.hrefs


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
    opener: Opener = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> RemoteImage:
    """挑选最新候选；``mtime`` 依赖 HEAD 的 Last-Modified，失败时回退文件名。"""
    if not candidates:
        raise ImageFetchError("Empty image candidate list")
    if order_by == "mtime":
        stamped = [(mtime_of(item.url, opener=opener), item) for item in candidates]
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
    opener: Opener = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> float | None:
    """HEAD 请求的 Last-Modified 时间戳；不支持或失败时返回 None。"""

    def head() -> float | None:
        with opener(url, method="HEAD") as response:
            headers = response.headers
            value = headers.get("Last-Modified")  # type: ignore[union-attr]
        if not value:
            return None
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return None

    try:
        result = _attempt(url, head, sleep=sleep)
    except (_PermanentError, AttributeError):
        return None
    return result if isinstance(result, float) else None


# --------------------------------------------------------------------------- #
# 目标解析与校验和
# --------------------------------------------------------------------------- #


def resolve_target(
    source: ImageSource,
    *,
    opener: Opener = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> RemoteTarget:
    """把配置中的镜像源解析为可下载目标。

    - ``url`` 种：直接使用配置的候选直链；
    - ``index`` 种：拉取目录页并按 ``match`` 选最新，同时定位校验清单。
    """
    if source.kind == "url":
        filename = basename(source.urls[0])
        if not filename:
            raise ImageFetchError(
                f"Image source {source.id} has an invalid URL: {source.urls[0]}"
            )
        return RemoteTarget(
            filename=filename,
            urls=tuple(source.urls),
            checksum_url=source.checksum_url or None,
            version=version_of(filename),
            algo=source.checksum_algo,
        )

    failures: list[str] = []
    for page in source.pages:
        try:
            html = fetch_text((page,), opener=opener, sleep=sleep)
        except ImageFetchError as exc:
            failures.append(f"{page} ({exc})")
            continue
        candidates = parse_index(html, page, source.match or "")
        if not candidates:
            failures.append(f"{page} (no file matched {source.match!r})")
            continue
        chosen = pick_latest(candidates, source.order_by, opener=opener, sleep=sleep)
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


def _match_checksum_name(html: str, pattern: str) -> str | None:
    if not pattern:
        return None
    regex = re.compile(pattern)
    for href in extract_hrefs(html):
        name = basename(href)
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
    opener: Opener = _default_opener,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """拉取并解析目标文件的校验和；没有清单地址时返回 None。"""
    if not target.checksum_url:
        return None
    text = fetch_text((target.checksum_url,), opener=opener, sleep=sleep)
    return parse_checksum(text, target.filename, target.algo)


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #


def _free_space(directory: Path) -> int:
    stats = os.statvfs(directory)
    return stats.f_bavail * stats.f_frsize


def download(
    target: RemoteTarget,
    part_path: Path,
    *,
    algo: str = "sha256",
    expected: str | None = None,
    opener: Opener = _default_opener,
    progress: Progress | None = None,
    free_space: Callable[[Path], int] = _free_space,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """流式下载到 ``part_path``，同时增量计算哈希。

    校验失败会删除临时文件并重新尝试；全部候选失败时抛 ImageFetchError。
    正式镜像只有在调用方校验通过并原子重命名后才出现。
    """
    failures: list[str] = []
    for url in target.urls:
        for attempt in range(RETRIES):
            try:
                return _download_once(
                    url,
                    part_path,
                    algo=algo,
                    expected=expected,
                    opener=opener,
                    progress=progress,
                    free_space=free_space,
                )
            except _ChecksumMismatch as exc:
                failures.append(str(exc))
                _unlink(part_path)
            except _PermanentError as exc:
                failures.append(f"{url} ({exc})")
                break
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                failures.append(f"{url} ({_describe(exc)})")
                _unlink(part_path)
            if attempt + 1 < RETRIES:
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise ImageFetchError(
        f"Failed to download {target.filename}: " + "; ".join(failures)
    )


class _ChecksumMismatch(Exception):
    pass


def _download_once(
    url: str,
    part_path: Path,
    *,
    algo: str,
    expected: str | None,
    opener: Opener,
    progress: Progress | None,
    free_space: Callable[[Path], int],
) -> DownloadResult:
    _unlink(part_path)
    digest = hashlib.new(algo)
    written = 0
    try:
        with opener(url) as response:
            total = _content_length(response)
            if total is not None and total > free_space(part_path.parent):
                raise ImageFetchError(
                    f"Not enough free disk space for {part_path.name}: "
                    f"need {total} bytes"
                )
            with open(part_path, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    data = bytes(chunk)
                    handle.write(data)
                    digest.update(data)
                    written += len(data)
                    if progress is not None:
                        progress(written, total)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in (408, 429):
            raise _PermanentError(_describe(exc)) from exc
        raise
    except urllib.error.URLError:
        raise

    actual = digest.hexdigest()
    if expected is not None and actual != expected.lower():
        raise _ChecksumMismatch(
            f"{url}: checksum mismatch (expected {expected}, got {actual})"
        )
    return DownloadResult(checksum=actual, size=written)


def _content_length(response: _Response) -> int | None:
    headers = response.headers
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter("Content-Length")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# 本地状态（images/.state.json）
# --------------------------------------------------------------------------- #


def load_state(image_dir: Path) -> dict[str, dict[str, object]]:
    """读取下载状态；文件缺失或损坏时返回空表并给出警告。"""
    path = image_dir / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(
            f"Warning: ignoring unreadable image state file {path}",
            file=sys.stderr,
        )
        return {}
    if not isinstance(raw, dict):
        print(
            f"Warning: ignoring invalid image state file {path}",
            file=sys.stderr,
        )
        return {}
    state: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            state[key] = value
    return state


def save_state(image_dir: Path, state: dict[str, dict[str, object]]) -> None:
    """原子写入状态文件（先写临时文件再 os.replace）。"""
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / STATE_FILENAME
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
