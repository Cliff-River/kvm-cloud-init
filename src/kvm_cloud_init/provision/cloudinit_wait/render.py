"""终端渲染：verbose 固定区域、进度行与失败摘要。"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from ... import ssh
from ...errors import CloudInitError
from . import query
from .constants import (
    OUTPUT_LOG,
    ROWS,
    SPINNER_FRAMES,
    SPINNER_INTERVAL,
    STATUS_LONG_CMD,
    VERBOSE_LINES,
)


class _VerboseRenderer:
    """固定 7 行区域滚动显示 cloud-init 输出。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._rendered = 0
        self._seen: set[str] = set()
        self._frame = 0
        self._width = shutil.get_terminal_size((80, 24)).columns

    @property
    def is_tty(self) -> bool:
        return self._is_tty

    def update(self, status: str, lines: list[str]) -> None:
        lines = lines[-VERBOSE_LINES:]
        if not self._is_tty:
            for line in lines:
                if line not in self._seen:
                    self._seen.add(line)
                    print(line, flush=True)
            return

        rows = [
            f"cloud-init: {SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]} {status}",
            *lines,
        ]
        self._frame += 1
        rows.extend([""] * (ROWS - len(rows)))
        # 截断到终端宽度，避免长日志换行破坏 7 行重绘的定位
        rows = [row[: max(1, self._width - 1)] for row in rows]
        if self._rendered:
            print(f"\x1b[{self._rendered}A", end="", file=self._stream)
        for row in rows:
            print(f"\x1b[2K{row}", file=self._stream)
        self._stream.flush()  # type: ignore[attr-defined]
        self._rendered = ROWS

    def clear(self) -> None:
        if self._is_tty and self._rendered:
            print(
                f"\x1b[{self._rendered}A\x1b[J",
                end="",
                file=self._stream,
            )
            self._stream.flush()  # type: ignore[attr-defined]
        self._rendered = 0


class _ProgressLine:
    """TTY 下用单行原地刷新进度，非 TTY 退化为逐行追加。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._active = False

    @property
    def is_tty(self) -> bool:
        return self._is_tty

    def update(self, text: str) -> None:
        if not self._is_tty:
            print(text, flush=True)
            return
        prefix = "\r\x1b[2K" if self._active else ""
        self._stream.write(f"{prefix}{text}")  # type: ignore[attr-defined]
        self._stream.flush()  # type: ignore[attr-defined]
        self._active = True

    def animate(
        self,
        seconds: float,
        status: str,
        started: float,
        *,
        label: str = "Cloud-init status",
    ) -> None:
        """在等待轮询期间持续刷新转圈动画。"""
        steps = max(1, round(seconds / SPINNER_INTERVAL))
        for index in range(steps):
            now = time.monotonic()
            frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
            self.update(
                f"{frame} {label}: {status} "
                f"({int(now - started)}s elapsed)"
            )
            time.sleep(SPINNER_INTERVAL)

    def clear(self) -> None:
        if self._is_tty and self._active:
            self._stream.write("\r\x1b[2K")  # type: ignore[attr-defined]
            self._stream.flush()  # type: ignore[attr-defined]
            self._active = False


def _print_failure(name: str, target: tuple[str, str, Path]) -> None:
    print("Cloud-init failed:", file=sys.stderr)
    try:
        long_out, long_err, _code = query._capture(name, target, STATUS_LONG_CMD)
        detail = (long_out or long_err).strip()
        if detail:
            print(detail, file=sys.stderr)
        tail = query._tail_lines(name, target, OUTPUT_LOG, 20)
        if tail:
            print(f"Last 20 lines of {OUTPUT_LOG}:", file=sys.stderr)
            for line in tail:
                print(line, file=sys.stderr)
    except (ssh.SshError, CloudInitError) as exc:
        print(f"Could not read cloud-init details: {exc}", file=sys.stderr)
