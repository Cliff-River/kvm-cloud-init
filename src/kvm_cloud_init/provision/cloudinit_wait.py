"""等待 cloud-init、查询状态与跟随日志。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import libvirt

from .. import ssh
from ..errors import CloudInitError, NetworkError
from . import access

#: 查询状态与等待 cloud-init 的轮询间隔（秒）
POLL_INTERVAL = 2
#: verbose 模式刷新间隔（秒）
VERBOSE_INTERVAL = 1
#: 默认模式进度心跳间隔（秒）
PROGRESS_INTERVAL = 15
#: TTY 转圈动画帧与刷新间隔
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_INTERVAL = 0.1
#: 等待 SSH 可达的最长时间（秒）
SSH_READY_TIMEOUT = 300
#: verbose 区域最多显示 7 行：1 行状态 + 6 行输出
VERBOSE_LINES = 6
ROWS = 7

STATUS_JSON_CMD = "cloud-init status --format=json"
STATUS_LONG_CMD = "cloud-init status --long"
LOG_FILE = "/var/log/cloud-init.log"
OUTPUT_LOG = "/var/log/cloud-init-output.log"


def _capture(
    name: str, target: tuple[str, str, Path], command: str
) -> tuple[str, str, int]:
    host, user, key_path = target
    return ssh.capture_command(name, host, user, key_path, command)


def _query_state(name: str, target: tuple[str, str, Path]) -> str:
    """读取 ``cloud-init status --format=json`` 的 status 字段。"""
    out, err, _code = _capture(name, target, STATUS_JSON_CMD)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        detail = err.strip() or out.strip() or "empty output"
        raise CloudInitError(
            f"Cannot parse 'cloud-init status' output: {detail}"
        ) from exc
    state = data.get("status")
    if not isinstance(state, str):
        raise CloudInitError(
            f"'cloud-init status' output has no string status field: {out.strip()}"
        )
    return state


def _tail_lines(
    name: str, target: tuple[str, str, Path], path: str, count: int
) -> list[str]:
    out, _err, _code = _capture(name, target, f"tail -n {count} {path}")
    return out.rstrip("\n").splitlines()


def _wait_target(
    conn: libvirt.virConnect,
    name: str,
    deadline: float,
    progress: _ProgressLine | None = None,
    started: float = 0.0,
) -> tuple[str, str, Path]:
    """轮询解析 SSH 目标，直到 IP 可用或超时。"""
    while True:
        try:
            return access.ssh_target(conn, name)
        except NetworkError as exc:
            if time.monotonic() >= deadline:
                raise CloudInitError(
                    f"Instance {name} did not become reachable over SSH within "
                    f"{SSH_READY_TIMEOUT}s; use --no-wait to skip cloud-init waiting"
                ) from exc
            if progress is not None and progress.is_tty:
                progress.animate(
                    POLL_INTERVAL,
                    "waiting for connection",
                    started,
                    label="SSH",
                )
            else:
                time.sleep(POLL_INTERVAL)


class _VerboseRenderer:
    """固定 7 行区域滚动显示 cloud-init 输出。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._rendered = 0
        self._seen: set[str] = set()
        self._frame = 0

    @property
    def is_tty(self) -> bool:
        return self._is_tty

    def update(self, status: str, lines: list[str]) -> None:
        lines = lines[-VERBOSE_LINES:]
        if not self._is_tty:
            for line in lines:
                if line not in self._seen:
                    self._seen.add(line)
                    print(line)
            return

        rows = [
            f"cloud-init: {SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]} {status}",
            *lines,
        ]
        self._frame += 1
        rows.extend([""] * (ROWS - len(rows)))
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
            print(text)
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
        long_out, long_err, _code = _capture(name, target, STATUS_LONG_CMD)
        detail = (long_out or long_err).strip()
        if detail:
            print(detail, file=sys.stderr)
        tail = _tail_lines(name, target, OUTPUT_LOG, 20)
        if tail:
            print(f"Last 20 lines of {OUTPUT_LOG}:", file=sys.stderr)
            for line in tail:
                print(line, file=sys.stderr)
    except ssh.SshError as exc:
        print(f"Could not read cloud-init details: {exc}", file=sys.stderr)


def wait_cloud_init(
    conn: libvirt.virConnect, name: str, *, verbose: bool = False
) -> int:
    """等待 cloud-init 结束，返回 0（成功/用户中断）或 1（失败）。"""
    started = time.monotonic()
    deadline = started + SSH_READY_TIMEOUT
    renderer = _VerboseRenderer(sys.stdout)
    progress = _ProgressLine(sys.stdout)
    target: tuple[str, str, Path] | None = None
    state = "not started"
    last_check = 0.0
    last_progress = started
    connected = False

    print("Waiting for cloud-init to complete. Press Ctrl+C to stop waiting.")

    try:
        while True:
            now = time.monotonic()
            if target is None:
                target = _wait_target(
                    conn, name, deadline, progress, started
                )
                if not connected:
                    progress.clear()
                    print(
                        f"Connected to {name} at {target[0]}. "
                        "Polling cloud-init status..."
                    )
                    connected = True

            # 动画分帧累加 0.1s 会有浮点误差，留 1ms 容差
            if now - last_check >= POLL_INTERVAL - 0.001:
                try:
                    state = _query_state(name, target)
                except ssh.SshError:
                    target = None
                    if now >= deadline:
                        raise CloudInitError(
                            f"Could not connect to instance {name} within "
                            f"{SSH_READY_TIMEOUT}s; use --no-wait to skip "
                            "cloud-init waiting"
                        )
                    if progress.is_tty:
                        progress.animate(
                            POLL_INTERVAL,
                            "waiting for connection",
                            started,
                            label="SSH",
                        )
                    else:
                        time.sleep(POLL_INTERVAL)
                    continue
                last_check = now

            if verbose:
                try:
                    lines = _tail_lines(name, target, OUTPUT_LOG, VERBOSE_LINES)
                except (ssh.SshError, CloudInitError):
                    lines = []
                renderer.update(state, lines)

            if now - last_progress >= PROGRESS_INTERVAL:
                # TTY 已有转圈/滚动区域，只在非 TTY 追加心跳
                if not renderer.is_tty and state not in ("done", "error"):
                    progress.update(
                        f"Cloud-init status: {state} "
                        f"({int(now - started)}s elapsed)"
                    )
                last_progress = now

            if state == "done":
                renderer.clear()
                progress.clear()
                elapsed = int(time.monotonic() - started)
                print(f"Cloud-init completed successfully in {elapsed}s.")
                return 0
            if state == "error":
                renderer.clear()
                progress.clear()
                _print_failure(name, target)
                return 1

            if verbose:
                time.sleep(VERBOSE_INTERVAL)
            elif progress.is_tty:
                progress.animate(POLL_INTERVAL, state, started)
            else:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        renderer.clear()
        progress.clear()
        print(
            f"Stopped waiting for cloud-init. Instance {name} keeps running."
        )
        return 0


def cloud_init_status(
    conn: libvirt.virConnect, name: str
) -> int:
    """打印 cloud-init 状态；done=0，running/not started=2，error=1。"""
    target = access.ssh_target(conn, name)
    state = _query_state(name, target)

    out, err, _code = _capture(name, target, STATUS_LONG_CMD)
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip(), file=sys.stderr)

    if state == "done":
        return 0
    if state == "error":
        return 1
    return 2


def cloud_init_log(
    conn: libvirt.virConnect, name: str, *, follow: bool = False
) -> int:
    """输出 cloud-init 日志；``follow`` 时实时跟随到 cloud-init 结束。"""
    target = access.ssh_target(conn, name)

    if not follow:
        return _print_log(name, target)

    state = _query_state(name, target)
    if state in ("done", "error"):
        _print_log(name, target)
        return 0 if state == "done" else 1

    holder = {"state": state, "checked": 0.0}

    def should_stop() -> bool:
        now = time.monotonic()
        if now - holder["checked"] >= POLL_INTERVAL:
            try:
                holder["state"] = _query_state(name, target)
            except ssh.SshError:
                pass
            holder["checked"] = now
        return holder["state"] in ("done", "error")

    host, user, key_path = target
    ssh.stream_command(
        name,
        host,
        user,
        key_path,
        f"tail -n +1 -F {LOG_FILE}",
        should_stop=should_stop,
    )
    return 0 if holder["state"] == "done" else 1


def _print_log(name: str, target: tuple[str, str, Path]) -> int:
    out, err, code = _capture(name, target, f"cat {LOG_FILE}")
    sys.stdout.write(out)
    sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return code
