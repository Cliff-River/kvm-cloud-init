"""终端进度输出：rich 进度条与缩放自愈渲染。

只做表现层，不含镜像目录或下载逻辑；编排层通过 ``imagefetch.Progress``
协议接收进度回调，由调用方（cli / create）决定是否传入本模块的实现。
"""

from __future__ import annotations

import sys
import time
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

from ..imagefetch import Progress

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
