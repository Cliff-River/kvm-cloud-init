"""云镜像下载的纯能力层：目录页解析、校验和、流式下载与本地状态。

本模块不依赖 config / provision / hv，只做 I/O 与解析，便于单元测试时注入
``httpx.Client(transport=...)``，全程不触网。

设计要点：

- HTTP 统一走 httpx（连接池、重定向、超时集中配置）；HTML 用 parsel 解析；
- 不调用 curl/wget 子进程；
- 下载写入 ``.<file>.part`` 临时文件，校验通过后原子重命名；
- 压缩包（``.tar.xz`` 等）由标准库 ``tarfile`` / ``zipfile`` 解出唯一的
  普通文件，全零块按稀疏写入，不产生等量的磁盘占用；
- 校验清单同时支持 coreutils（``<hash>  <file>``）与 BSD
  （``SHA256 (<file>) = <hash>``）两种格式，以及 Fedora 的 PGP 包裹清单。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tarfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx
from parsel import Selector

from . import __version__
from .errors import ImageFetchError

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from typing import BinaryIO

    from .config import ImageSource

STATE_FILENAME = ".state.json"
CHUNK_SIZE = 1024 * 1024
TIMEOUT = 30
RETRIES = 3
BACKOFF = (2.0, 4.0)
USER_AGENT = f"kvmci/{__version__}"

_HASH_LENGTHS = {"sha256": 64, "sha512": 128}
#: 支持解压的发布物后缀（Kali 的云镜像就是 .tar.xz 包）
ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tgz", ".tar", ".zip")
_EXTENSIONS = ARCHIVE_SUFFIXES + (".qcow2", ".img", ".raw")
_TAR_MODES = {
    ".tar.xz": "r:xz",
    ".tar.gz": "r:gz",
    ".tgz": "r:gz",
    ".tar.bz2": "r:bz2",
    ".tar": "r:",
}
_COREUTILS = re.compile(r"^([0-9a-fA-F]+)\s+\*?(.+?)\s*$")
_BSD = re.compile(r"^[A-Za-z0-9]+ \((.+)\) = ([0-9a-fA-F]+)\s*$")

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


# --------------------------------------------------------------------------- #
# 基础 HTTP
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# 目录页解析
# --------------------------------------------------------------------------- #


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
        http, owned = _use_client(client)
        try:
            stamped = [
                (mtime_of(item.url, client=http, sleep=sleep), item)
                for item in candidates
            ]
        finally:
            if owned:
                http.close()
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
    http, owned = _use_client(client)
    try:

        def head() -> float | None:
            response = http.head(url)
            _check_response(response)
            value = response.headers.get("Last-Modified")
            if not value:
                return None
            try:
                return parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError):
                return None

        try:
            result = _attempt(url, head, sleep=sleep)
        except _PermanentError:
            return None
        return result if isinstance(result, float) else None
    finally:
        if owned:
            http.close()


# --------------------------------------------------------------------------- #
# 目标解析与校验和
# --------------------------------------------------------------------------- #


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

    http, owned = _use_client(client)
    try:
        failures: list[str] = []
        for page in source.pages:
            try:
                html = fetch_text((page,), client=http, sleep=sleep)
            except ImageFetchError as exc:
                failures.append(f"{page} ({exc})")
                continue
            candidates = parse_index(html, page, source.match or "")
            if not candidates:
                failures.append(f"{page} (no file matched {source.match!r})")
                continue
            chosen = pick_latest(
                candidates, source.order_by, client=http, sleep=sleep
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
            http.close()


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
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """拉取并解析目标文件的校验和；没有清单地址时返回 None。"""
    if not target.checksum_url:
        return None
    text = fetch_text((target.checksum_url,), client=client, sleep=sleep)
    return parse_checksum(text, target.filename, target.algo)


# --------------------------------------------------------------------------- #
# 压缩包解压
# --------------------------------------------------------------------------- #


def is_archive(filename: str) -> bool:
    """判断发布物是否为受支持的压缩包。"""
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def extract_archive(archive: Path, dest: Path, *, filename: str | None = None) -> str:
    """把压缩包内唯一的普通文件解到 ``dest``，返回包内文件名。

    只支持单文件包（Kali 的 ``.tar.xz`` 里就是 ``disk.raw``）：多文件说明
    发布物结构超出预期，直接失败以免解出错误的镜像。解压时全零块改用
    ``seek`` 前进，裸盘的稀疏区域不会真的占用磁盘。

    ``filename`` 是发布物原始文件名：下载中的临时文件以 ``.part`` 结尾，
    无法据此判断压缩格式，故由调用方传入上游名字。
    """
    name = filename or archive.name
    lowered = name.lower()
    with contextlib.ExitStack() as stack:
        if lowered.endswith(".zip"):
            zf = stack.enter_context(zipfile.ZipFile(archive))
            names = [info.filename for info in zf.infolist() if not info.is_dir()]
            member = _only_member(names, name)
            stream: BinaryIO = stack.enter_context(zf.open(member))
        else:
            mode = next(
                (value for suffix, value in _TAR_MODES.items()
                 if lowered.endswith(suffix)),
                None,
            )
            if mode is None:
                raise ImageFetchError(
                    f"Unsupported archive format: {name}"
                )
            tf = stack.enter_context(tarfile.open(archive, mode))
            member = _only_member(
                [item.name for item in tf.getmembers() if item.isreg()], name
            )
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ImageFetchError(
                    f"Cannot read {member} from archive {name}"
                )
            stream = stack.enter_context(extracted)
        _write_sparse(stream, dest)
    return member


def _only_member(names: list[str], archive_name: str) -> str:
    if len(names) != 1:
        raise ImageFetchError(
            f"Archive {archive_name} must contain exactly one file, found "
            f"{len(names)}: {', '.join(names[:5]) or 'none'}"
        )
    return names[0]


def _write_sparse(stream: BinaryIO, dest: Path) -> None:
    """边读边写；整块全零时只前进写入位置，保持文件稀疏。"""
    with dest.open("wb") as out:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            if chunk.strip(b"\x00"):
                out.write(chunk)
            else:
                out.seek(len(chunk), os.SEEK_CUR)
        out.truncate()


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
    client: Client | None = None,
    progress: Progress | None = None,
    free_space: Callable[[Path], int] = _free_space,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    """流式下载到 ``part_path``，同时增量计算哈希。

    校验失败会删除临时文件并重新尝试；全部候选失败时抛 ImageFetchError。
    正式镜像只有在调用方校验通过并原子重命名后才出现。
    """
    http, owned = _use_client(client)
    try:
        failures: list[str] = []
        for url in target.urls:
            # 每次重试从头计数，避免进度条在重置后倒退
            if progress is not None:
                progress.update(0, None)
            for attempt in range(RETRIES):
                try:
                    return _download_once(
                        http,
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
                    failures.append(f"{url} ({_describe(exc)})")
                    _unlink(part_path)
                except KeyboardInterrupt:
                    # Ctrl+C 取消时清掉半成品，正式镜像始终保持不变
                    _unlink(part_path)
                    raise
                if attempt + 1 < RETRIES:
                    sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        raise ImageFetchError(
            f"Failed to download {target.filename}: " + "; ".join(failures)
        )
    finally:
        if progress is not None:
            progress.close()
        if owned:
            http.close()


class _ChecksumMismatch(Exception):
    pass


def _download_once(
    http: Client,
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
    with http.stream("GET", url) as response:
        _check_response(response)
        total = _content_length(response)
        if total is None:
            # 部分源（如 AlmaLinux）用 chunked 传输，GET 无 Content-Length；
            # HEAD 通常仍返回准确大小，用于进度与磁盘空间预检。
            total = _remote_size(http, url)
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


def _remote_size(http: Client, url: str) -> int | None:
    """用 HEAD 获取文件总大小；不支持或失败时返回 None（进度显示当前值）。"""
    try:
        response = http.head(url)
        _check_response(response)
    except (httpx.HTTPError, _PermanentError):
        return None
    return _content_length(response)


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
