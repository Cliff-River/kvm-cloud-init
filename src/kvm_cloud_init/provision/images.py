"""镜像目录编排：初始化、刷新、列出、探测与拉取，以及 create 的镜像保障。"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from .. import catalog, imagefetch
from ..config import ConfigStore, ImageSource
from ..errors import ConfigError, ImageFetchError, ImageNotFound

Client = imagefetch.Client
Progress = imagefetch.Progress


# --------------------------------------------------------------------------- #
# 目录初始化 / 刷新
# --------------------------------------------------------------------------- #


def init_catalog(root: Path, *, force: bool = False) -> int:
    """从内置预设生成 images.conf；已存在时默认拒绝覆盖。"""
    path = root / catalog.CATALOG_FILENAME
    if path.exists() and not force:
        print(
            f"Error: {path} already exists; use 'kvmci images refresh' to rebuild it "
            "or add --force to overwrite.",
            file=sys.stderr,
        )
        return 1
    _write_catalog(path, catalog.render_catalog())
    print(f"Generated image catalog: {path}")
    return 0


def refresh_catalog(
    root: Path,
    *,
    include_archived: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """重建 images.conf，并把上游最新文件名/版本写回生成文件的元数据字段。"""
    probes: dict[str, dict[str, str]] = {}
    attempted = 0
    reachable = 0
    for entry in catalog.PRESET:
        if entry.archived and not include_archived:
            continue
        attempted += 1
        source = _preset_source(entry)
        try:
            target = imagefetch.resolve_target(source, client=client, sleep=sleep)
            checksum = imagefetch.fetch_checksum(target, client=client, sleep=sleep)
        except ImageFetchError as exc:
            probes[entry.id] = {
                "upstream_status": "unreachable",
                "checked_at": _timestamp(),
            }
            print(f"{entry.id}: unreachable ({exc})")
            continue
        reachable += 1
        probes[entry.id] = {
            "upstream_file": target.filename,
            "upstream_version": target.version,
            "upstream_status": "ok",
            "checked_at": _timestamp(),
        }
        suffix = f" {checksum[:12]}" if checksum else ""
        print(f"{entry.id}: {target.filename}{suffix}")

    path = root / catalog.CATALOG_FILENAME
    _write_catalog(path, catalog.render_catalog(probes))
    print(f"Refreshed image catalog: {path} ({reachable}/{attempted} sources reachable)")
    return 1 if attempted and reachable == 0 else 0


def _preset_source(entry: catalog.PresetEntry) -> ImageSource:
    return ImageSource(
        id=entry.id,
        file=entry.file,
        kind=entry.kind,
        urls=entry.urls,
        pages=entry.pages,
        match=entry.match or None,
        checksum_url=entry.checksum_url or None,
        checksum_file=entry.checksum_file or None,
        checksum_match=entry.checksum_match or None,
        checksum_algo=entry.checksum_algo,
        archived=entry.archived,
        arch=entry.arch,
    )


def _write_catalog(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------- #
# list / check / pull
# --------------------------------------------------------------------------- #


def list_images(store: ConfigStore) -> list[tuple[str, str, str, str, str, str]]:
    """返回 (ID, Origin, Local, Kind, Catalog ver., Local ver.) 行，不触网。"""
    state = imagefetch.load_state(store.image_dir)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for image_id, source in store.images.items():
        disk = store.image_path(image_id)
        local_version = _state_value(state, image_id, "version")
        rows.append(
            (
                image_id,
                source.origin,
                "present" if disk.is_file() else "missing",
                source.kind,
                source.upstream_version or "-",
                local_version or "-",
            )
        )
    return rows


def check_images(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_entries: bool = False,
    include_archived: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """只探测上游，不下载；存在可更新或失败时返回 1。"""
    ids = _select_ids(
        store,
        tokens,
        all_entries=all_entries,
        all_templates=not tokens and not all_entries,
        include_archived=include_archived,
    )
    state = imagefetch.load_state(store.image_dir)
    failed = False
    for image_id in ids:
        source = store.images[image_id]
        disk = store.image_path(image_id)
        local_version = _state_value(state, image_id, "version") or "-"
        try:
            target = imagefetch.resolve_target(source, client=client, sleep=sleep)
            expected = _expected_checksum(source, target, client=client, sleep=sleep)
        except ImageFetchError as exc:
            print(f"{image_id}: unreachable ({exc})")
            failed = True
            continue
        upstream = source.upstream_version or target.version
        if not disk.is_file():
            print(f"{image_id}: missing (upstream {upstream})")
            failed = True
            continue
        if expected is None:
            print(f"{image_id}: unknown (no checksum; local {local_version})")
            continue
        if _hash_file(disk, source.checksum_algo) == expected:
            print(f"{image_id}: up to date ({local_version})")
        else:
            print(
                f"{image_id}: update available "
                f"(local {local_version}, upstream {upstream})"
            )
            failed = True
    return 1 if failed else 0


def pull_images(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_templates: bool = False,
    force: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress_factory: Callable[[str], Progress | None] = lambda name: progress_printer(
        name
    ),
) -> int:
    """下载或更新指定镜像；单项失败不中断，全部处理完后返回退出码。"""
    ids = _select_ids(store, tokens, all_templates=all_templates)
    failures: list[str] = []
    for image_id in ids:
        try:
            _pull_one(
                store,
                image_id,
                force=force,
                client=client,
                sleep=sleep,
                progress=progress_factory(image_id),
            )
        except (ImageFetchError, ImageNotFound, OSError) as exc:
            failures.append(image_id)
            print(f"Error: failed to pull {image_id}: {exc}", file=sys.stderr)
    if len(ids) > 1:
        print(f"Images processed: {len(ids) - len(failures)} ok, {len(failures)} failed.")
    return 1 if failures else 0


def _pull_one(
    store: ConfigStore,
    image_id: str,
    *,
    force: bool,
    client: Client | None,
    sleep: Callable[[float], None],
    progress: Progress | None,
) -> None:
    source = store.images[image_id]
    disk = store.image_path(image_id)
    target = imagefetch.resolve_target(source, client=client, sleep=sleep)
    expected = _expected_checksum(source, target, client=client, sleep=sleep)

    if disk.is_file() and not force:
        if expected is not None and _hash_file(disk, source.checksum_algo) == expected:
            print(f"Image {image_id} is up to date ({disk.name}).")
            return
        if expected is None:
            state = imagefetch.load_state(store.image_dir)
            if _state_value(state, image_id, "upstream_file") == target.filename:
                print(f"Image {image_id} is up to date ({disk.name}).")
                return

    _download(store, source, target, expected, progress=progress, client=client)


def ensure_image(
    store: ConfigStore,
    image_id: str,
    *,
    policy: str | None = None,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Progress | None = None,
) -> Path:
    """确保镜像在本地存在：缺失时按策略自动下载，或按 local 策略直接失败。"""
    disk = store.image_path(image_id)
    if disk.is_file():
        return disk

    effective = policy or store.defaults.image_policy
    if effective == "local":
        raise ImageNotFound(
            f"Source image does not exist: {disk} (image {image_id}); "
            f"run 'kvmci images pull {image_id}' or set image_policy: auto"
        )

    source = store.images[image_id]
    target = imagefetch.resolve_target(source, client=client, sleep=sleep)
    expected = _expected_checksum(source, target, client=client, sleep=sleep)
    print(f"Image {image_id} is missing locally; downloading {target.filename} ...")
    return _download(store, source, target, expected, progress=progress, client=client)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _select_ids(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_entries: bool = False,
    all_templates: bool = False,
    include_archived: bool = False,
) -> list[str]:
    """把镜像 ID / 模板名解析为镜像 ID 列表（去重、保持顺序）。"""
    if all_entries:
        return [
            image_id
            for image_id, source in store.images.items()
            if include_archived or not source.archived
        ]
    if all_templates:
        return store.template_image_ids()
    chosen: list[str] = []
    for token in tokens:
        if token in store.images:
            image_id = token
        elif token in store.templates:
            image_id = store.templates[token].image
        else:
            raise ConfigError(
                f"Unknown image or template: {token} (see 'kvmci images list')"
            )
        if image_id not in chosen:
            chosen.append(image_id)
    return chosen


def _expected_checksum(
    source: ImageSource,
    target: imagefetch.RemoteTarget,
    *,
    client: Client | None,
    sleep: Callable[[float], None],
) -> str | None:
    try:
        checksum = imagefetch.fetch_checksum(target, client=client, sleep=sleep)
    except ImageFetchError:
        if source.insecure:
            print(
                f"Warning: no checksum available for {source.id}; continuing "
                "because insecure is enabled.",
                file=sys.stderr,
            )
            return None
        raise
    if checksum is None:
        if source.insecure:
            print(
                f"Warning: no checksum configured for {source.id}; continuing "
                "because insecure is enabled.",
                file=sys.stderr,
            )
            return None
        raise ImageFetchError(
            f"Image {source.id} has no checksum source; set insecure: true to "
            "download without verification"
        )
    return checksum


def _download(
    store: ConfigStore,
    source: ImageSource,
    target: imagefetch.RemoteTarget,
    expected: str | None,
    *,
    progress: Progress | None,
    client: Client | None,
) -> Path:
    disk = store.image_path(source.id)
    disk.parent.mkdir(parents=True, exist_ok=True)
    part = disk.with_name(f".{disk.name}.part")
    print(f"Downloading image {source.id} ({target.filename}) ...")
    result = imagefetch.download(
        target,
        part,
        algo=source.checksum_algo,
        expected=expected,
        client=client,
        progress=progress,
    )
    os.replace(part, disk)

    state = imagefetch.load_state(disk.parent)
    state[source.id] = {
        "file": disk.name,
        "url": target.urls[0],
        "version": target.version,
        "upstream_file": target.filename,
        "checksum_algo": source.checksum_algo,
        "checksum": result.checksum,
        "size": result.size,
        "fetched_at": _timestamp(),
    }
    imagefetch.save_state(disk.parent, state)
    print(f"Image {source.id} saved to {disk}.")
    return disk


def _hash_file(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(imagefetch.CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_value(
    state: dict[str, dict[str, object]], image_id: str, key: str
) -> str | None:
    entry = state.get(image_id)
    if not entry:
        return None
    value = entry.get(key)
    return str(value) if value is not None else None


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


PROGRESS_INTERVAL = 5.0
#: TTY 进度条的最小刷新间隔（秒），避免逐字节重绘拖慢下载
RENDER_INTERVAL = 0.1


class DownloadProgress:
    """下载进度输出。

    交互式终端（TTY）用 ``\\r`` + 清行在原地重绘单行进度条；非 TTY
    （重定向、CI）降级为每 5 秒追加一行 ``current/total`` 文本。

    终端缩放会触发 SIGWINCH：多行进度渲染会留下重排快照，
    这里在信号处理里按新宽度估算重排后的行数，逐行清除后再重绘。
    """

    def __init__(self, label: str, *, stream: TextIO | None = None) -> None:
        self._label = label
        self._stream = stream if stream is not None else sys.stdout
        self._interactive = self._stream.isatty()
        self._done = 0
        self._total: int | None = None
        self._reported = False
        self._last_report = 0.0
        self._closed = False
        self._started = time.monotonic()
        self._last_render = 0.0
        self._rendered = False
        self._last_length = 0
        self._previous_winch: object | None = None
        self._winch_installed = False
        if self._interactive:
            self._install_resize_handler()

    def update(self, done: int, total: int | None) -> None:
        if self._closed:
            return
        self._done = done
        self._total = total
        if done == 0:
            # 每次重试从头计数时重置速度/ETA 的基准时间
            self._started = time.monotonic()
        if self._interactive:
            finished = total is not None and done >= total
            self._render(force=finished)
        else:
            self._render_plain(done, total)

    def close(self) -> None:
        """结束进度输出：保留当前进度，不把它补到总量。"""
        if self._closed:
            return
        self._closed = True
        self._restore_resize_handler()
        if self._rendered:
            # 换行让后续输出另起一行；进度条本身停留在最后一次渲染的位置
            self._stream.write("\n")
            self._stream.flush()

    # ---- TTY 渲染 ---------------------------------------------------------- #

    def _install_resize_handler(self) -> None:
        if not hasattr(signal, "SIGWINCH"):
            return
        try:
            self._previous_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, OSError):
            # 非主线程无法注册信号处理器，退化为只在 update 时重绘
            return
        self._winch_installed = True

    def _restore_resize_handler(self) -> None:
        if not self._winch_installed:
            return
        self._winch_installed = False
        try:
            signal.signal(signal.SIGWINCH, self._previous_winch or signal.SIG_DFL)
        except (ValueError, OSError, TypeError):
            pass

    def _on_resize(self, signum: int, frame: object) -> None:
        if not self._rendered:
            return
        columns = _terminal_columns(self._stream)
        # 终端已按新宽度重排现有内容，需清掉全部重排行后重绘
        self._erase_rows(_visual_rows(self._last_length, columns))
        self._render(force=True)

    def _erase_rows(self, rows: int) -> None:
        chunks = ["\r\x1b[2K"]
        chunks.extend("\x1b[1A\r\x1b[2K" for _ in range(max(rows - 1, 0)))
        self._stream.write("".join(chunks))
        self._stream.flush()

    def _render(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_render < RENDER_INTERVAL:
            return
        self._last_render = now
        columns = _terminal_columns(self._stream)
        line = self._compose(max(columns - 1, 1))
        self._last_length = len(line)
        self._stream.write("\r\x1b[2K" + line)
        self._stream.flush()
        self._rendered = True

    def _compose(self, width: int) -> str:
        """按终端宽度拼一行：空间不足时依次舍弃 ETA、速度与进度条。"""
        done = self._done
        total = self._total
        head = [f"Downloading {self._label}:"]
        head.append(
            _fmt_size(done) if total is None
            else f"{_fmt_size(done)} / {_fmt_size(total)}"
        )
        if total:
            head.append(f"{done * 100 // total}%")
        head_text = " ".join(head)
        extras = [
            value
            for value in (self._speed_text(done, total), self._eta_text(done, total))
            if value
        ]
        extras_text = " ".join(extras)

        def bar_size(extra_len: int) -> int:
            # head 与进度条之间 1 个空格，尾部字段前再加 1 个空格
            reserved = len(head_text) + 1 + (1 + extra_len if extra_len else 0)
            return width - reserved

        while extras and bar_size(len(extras_text)) < 8:
            extras.pop()
            extras_text = " ".join(extras)
        size = bar_size(len(extras_text))
        if size < 8:
            # 实在太窄就只留文字，不再画进度条
            return (head_text + (f" {extras_text}" if extras_text else ""))[:width]
        line = f"{head_text} {self._bar(max(size - 2, 1))}"
        if extras_text:
            line += f" {extras_text}"
        return line[:width]

    def _bar(self, inner: int) -> str:
        total = self._total
        if not total:
            # 总量未知时显示来回移动的脉冲，而不是假的百分比
            position = int((time.monotonic() - self._started) * 8) % max(inner, 1)
            cells = ["-"] * inner
            cells[position] = "#"
            return "[" + "".join(cells) + "]"
        filled = min(inner, max(int(self._done / total * inner), 0))
        return "[" + "#" * filled + "-" * (inner - filled) + "]"

    def _speed_text(self, done: int, total: int | None) -> str:
        if total is None and done == 0:
            return ""
        elapsed = time.monotonic() - self._started
        if elapsed <= 0:
            return ""
        return f"{_fmt_size(int(done / elapsed))}/s"

    def _eta_text(self, done: int, total: int | None) -> str:
        if total is None or done >= total:
            return ""
        elapsed = time.monotonic() - self._started
        if elapsed <= 0 or done <= 0:
            return ""
        remaining = (total - done) / (done / elapsed)
        return f"ETA {_fmt_duration(remaining)}"

    # ---- 非 TTY 渲染 ------------------------------------------------------- #

    def _render_plain(self, done: int, total: int | None) -> None:
        finished = total is not None and done >= total
        if not self._reported:
            if total is None and done == 0:
                return
        elif not finished and time.monotonic() - self._last_report < PROGRESS_INTERVAL:
            return
        self._reported = True
        self._last_report = time.monotonic()
        print(self._text(done, total), file=self._stream, flush=True)

    def _text(self, done: int, total: int | None) -> str:
        if total is None:
            return f"Downloading {self._label}: {_fmt_size(done)}"
        percent = done * 100 // total if total else 100
        return (
            f"Downloading {self._label}: "
            f"{_fmt_size(done)} / {_fmt_size(total)} ({percent}%)"
        )


def progress_printer(label: str) -> Progress:
    """为指定镜像创建下载进度接收器。"""
    return DownloadProgress(label)


def _terminal_columns(stream: TextIO) -> int:
    """取当前终端宽度；无法查询时回退到 80。"""
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return shutil.get_terminal_size((80, 24)).columns


def _visual_rows(length: int, columns: int) -> int:
    """按终端宽度估算一段文本软换行后的行数。"""
    width = max(columns, 1)
    return max(1, (length + width - 1) // width)


def _fmt_size(size: int) -> str:
    mib = size / (1024 * 1024)
    return f"{mib:.1f} MiB"


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remaining = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{remaining:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
