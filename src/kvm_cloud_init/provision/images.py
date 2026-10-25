"""镜像目录编排：初始化、刷新、列出、探测与拉取，以及 create 的镜像保障。"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from rich import filesize
from rich import live as rich_live
from rich import progress as rich_progress
from rich.console import Console, ConsoleOptions, ConsoleRenderable
from rich.control import Control, ControlType
from rich.live_render import LiveRender
from rich.progress import (
    BarColumn,
    DownloadColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.segment import Segment
from rich.text import Text

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


class _ResizeAwareLive(rich_live.Live):
    """终端缩窄后能自愈的 Live。

    rich 每帧用 ``LiveRender._shape`` 记录的“旧宽度下的逻辑行数”把光标
    移回渲染起点再覆盖。终端缩窄时，已输出的物理行会被终端 reflow 成更多
    视觉行，rich 仍按旧行数上移光标，上方就留下重排快照。这里在每帧渲染
    前比较宽度，发现缩窄时先按“旧帧各行在新宽度下的视觉行数”逐行清除
    reflow 区域并复位光标，再交给 rich 按新宽度重绘。变宽时 rich 旧帧的
    行由显式换行分隔、不会被终端合并，其默认定位仍然正确，无需介入。
    rich 的自动刷新线程每 0.1 秒一帧，缩放后最迟下一帧即自愈，无需自行
    处理 SIGWINCH。

    注：终端模拟器若在物理缩放后、重排完成前才读到一帧旧宽度输出（写入
    端无法感知的极小调度窗口），该帧可能越过估算区域；这与 rich 等所有
    相对定位实现的固有边界一致，下一帧起恢复正常。
    """

    def process_renderables(
        self, renderables: list[ConsoleRenderable]
    ) -> list[ConsoleRenderable]:
        live_render = self._live_render
        shape = live_render._shape
        if shape is not None and self.console.is_interactive:
            old_width, old_height = shape
            new_width = self.console.size.width
            if old_width and 0 < new_width < old_width:
                visual_rows = self._reflowed_rows(live_render, old_width, new_width)
                if visual_rows != old_height:
                    renderables.insert(0, self._erase_control(visual_rows))
                    # 清空旧形状后，父类的 position_cursor 不再按旧行数上移
                    live_render._shape = None
        return super().process_renderables(renderables)

    def _reflowed_rows(
        self, live_render: LiveRender, old_width: int, new_width: int
    ) -> int:
        """用旧宽度重新布局当前帧，逐行估算缩窄后 reflow 的视觉行数。

        rich 在逻辑行之间输出的是显式换行，终端不会跨行合并，因此每个
        渲染行独立按新宽度折行后求和。
        """
        options: ConsoleOptions = self.console.options.update(width=old_width)
        style = self.console.get_style(live_render.style)
        lines = self.console.render_lines(
            live_render.renderable, options, style=style, pad=False
        )
        width = max(new_width, 1)
        return sum(
            max(1, (Segment.get_line_length(line) + width - 1) // width)
            for line in lines
        )

    @staticmethod
    def _erase_control(rows: int) -> Control:
        # 终端 reflow 后光标停在内容区最后一行行尾：先清该行，再上移 rows-1
        # 次逐行清除，恰好覆盖 rows 个视觉行并停在首行行首。不能多上移一次，
        # 否则会抹掉进度区上方已保留的普通输出（如下载提示行）。
        return Control(
            ControlType.CARRIAGE_RETURN,
            (ControlType.ERASE_IN_LINE, 2),
            *((ControlType.CURSOR_UP, 1), (ControlType.ERASE_IN_LINE, 2))
            * (rows - 1),
        )


class _DownloadSizeColumn(DownloadColumn):
    """总量未知时只显示已下载字节，避免 rich 默认渲染出 ``? MB``。"""

    def render(self, task: rich_progress.Task) -> Text:
        if task.total is None:
            return Text(
                filesize.decimal(int(task.completed)), style="progress.download"
            )
        return super().render(task)


class DownloadProgress:
    """下载进度输出。

    交互式终端（TTY）用 rich 渲染进度条、已下载/总量、速度与剩余时间；
    非 TTY（重定向、CI）降级为每 5 秒追加一行 ``current/total`` 文本。
    """

    def __init__(self, label: str, *, stream: TextIO | None = None) -> None:
        self._label = label
        self._stream = stream if stream is not None else sys.stdout
        self._interactive = self._stream.isatty()
        self._reported = False
        self._last_report = 0.0
        self._closed = False
        self._progress: rich_progress.Progress | None = None
        self._task_id: TaskID | None = None

    def update(self, done: int, total: int | None) -> None:
        if self._closed:
            return
        if self._interactive:
            self._render_rich(done, total)
        else:
            self._render_plain(done, total)

    def close(self) -> None:
        """结束进度输出；最终一帧保持当前进度，不会被补到总量。"""
        if self._closed:
            return
        self._closed = True
        if self._progress is not None:
            # stop() 会做最后一次刷新：显示的是当前 completed，而非 100%
            self._progress.stop()
            self._progress = None

    def _render_rich(self, done: int, total: int | None) -> None:
        if self._progress is None:
            console = Console(file=self._stream)
            progress = rich_progress.Progress(
                TextColumn("[bold]{task.description}"),
                BarColumn(bar_width=None),
                _DownloadSizeColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                auto_refresh=True,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            # 换成缩放自愈的 Live，参数与 Progress 内部构造保持一致
            progress.live = _ResizeAwareLive(
                console=console,
                auto_refresh=True,
                refresh_per_second=progress.live.refresh_per_second,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
                get_renderable=progress.get_renderable,
            )
            self._progress = progress
            progress.start()
            self._task_id = progress.add_task(
                f"Downloading {self._label}", total=total
            )
            return
        self._progress.update(self._task_id, completed=done, total=total)

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


def _fmt_size(size: int) -> str:
    mib = size / (1024 * 1024)
    return f"{mib:.1f} MiB"
