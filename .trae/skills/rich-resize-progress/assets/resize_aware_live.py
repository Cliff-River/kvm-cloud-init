"""可复制模板：缩放自愈的 rich 进度条。

依赖 rich>=13（基于 rich 15.x 验证）。TTY 下渲染彩条、已下载/总量、
速度、ETA，终端缩窄后下一帧自动清除 reflow 旧快照；非 TTY 降级为
定期追加文本行。

用法::

    progress = ResizeAwareProgress("myfile.bin")
    for chunk in response.iter_bytes():
        progress.update(done, total)  # total 未知时传 None
    progress.close()                # 取消时也只调 close()，最终帧停在当前进度

直接整文件复制进项目后按需要改标签、单位与节流间隔。
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

# 非 TTY 下降级文本行的最小输出间隔（秒）
PLAIN_INTERVAL = 5.0


class ResizeAwareLive(rich_live.Live):
    """终端缩窄后能自愈的 Live，变宽时保持 rich 原生定位。"""

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
        """用旧宽度重新布局当前帧，逐行估算缩窄后 reflow 的视觉行数。"""
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
        # 光标 reflow 后停在内容区最后一行行尾：先清该行，再上移 rows-1 次
        # 逐行清除，恰好覆盖 rows 个视觉行。上移 rows 次会误擦上方保留行。
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


class ResizeAwareProgress:
    """下载/长任务进度：TTY 用 rich（缩放自愈），非 TTY 追加节流文本行。"""

    def __init__(
        self,
        label: str,
        *,
        stream: TextIO | None = None,
        interval: float = PLAIN_INTERVAL,
    ) -> None:
        self._label = label
        self._stream = stream if stream is not None else sys.stdout
        self._interval = interval
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

    def refresh(self) -> None:
        """同步出一帧（主要给测试用，正式运行靠 rich 10Hz 自动刷新）。"""
        if self._progress is not None:
            self._progress.refresh()

    def close(self) -> None:
        """结束进度输出；最终一帧保持当前进度，不会被补到 100%。幂等。"""
        if self._closed:
            return
        self._closed = True
        if self._progress is not None:
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
            # 换成缩放自愈的 Live，参数与 Progress 内部构造保持一致；
            # 必须在 start() 之前替换。
            progress.live = ResizeAwareLive(
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
        elif not finished and time.monotonic() - self._last_report < self._interval:
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


def _fmt_size(size: int) -> str:
    """非 TTY 文本用二进制 MiB；rich 列内部用十进制 MB（与工具惯例一致）。"""
    mib = size / (1024 * 1024)
    return f"{mib:.1f} MiB"
