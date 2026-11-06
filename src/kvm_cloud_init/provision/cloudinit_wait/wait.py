"""等待 cloud-init 结束的主轮询循环。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import libvirt

from ... import ssh
from ...errors import CloudInitError, SshError
from .. import access
from ..access import SSH_READY_TIMEOUT
from . import query, render
from .constants import (
    LOG_FILE,
    OUTPUT_LOG,
    POLL_INTERVAL,
    PROGRESS_INTERVAL,
    VERBOSE_DEBUG_LINES,
    VERBOSE_INTERVAL,
    VERBOSE_OUTPUT_LINES,
)


def _wait_target(
    conn: libvirt.virConnect,
    name: str,
    deadline: float,
    progress: render._ProgressLine | None = None,
    started: float = 0.0,
) -> tuple[str, str, Path]:
    """轮询解析 SSH 目标，直到 IP 可用或超时。"""

    def on_wait(seconds: float) -> None:
        if progress is not None and progress.is_tty:
            progress.animate(seconds, "waiting for connection", started, label="SSH")
        else:
            time.sleep(seconds)

    try:
        return access.wait_ssh_target(
            conn, name, deadline=deadline, on_wait=on_wait
        )
    except SshError as exc:
        raise CloudInitError(
            f"Instance {name} did not become reachable over SSH within "
            f"{SSH_READY_TIMEOUT}s; use --no-wait to skip cloud-init waiting"
        ) from exc


def wait_cloud_init(
    conn: libvirt.virConnect, name: str, *, verbose: bool = False
) -> int:
    """等待 cloud-init 结束，返回 0（成功/用户中断）或 1（失败）。"""
    started = time.monotonic()
    deadline = started + SSH_READY_TIMEOUT
    verbose_renderer = render._VerboseRenderer(sys.stdout)
    progress = render._ProgressLine(sys.stdout)
    target: tuple[str, str, Path] | None = None
    state = "not started"
    last_check = 0.0
    last_progress = started
    connected = False

    print(
        "Waiting for cloud-init to complete. Press Ctrl+C to stop waiting.",
        flush=True,
    )

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
                        "Polling cloud-init status...",
                        flush=True,
                    )
                    connected = True

            # 动画分帧累加 0.1s 会有浮点误差，留 1ms 容差
            if now - last_check >= POLL_INTERVAL - 0.001:
                try:
                    state = query._query_state(name, target)
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
                lines: list[str] = []
                for path, count in (
                    (OUTPUT_LOG, VERBOSE_OUTPUT_LINES),
                    (LOG_FILE, VERBOSE_DEBUG_LINES),
                ):
                    try:
                        lines.extend(query._tail_lines(name, target, path, count))
                    except (ssh.SshError, CloudInitError) as exc:
                        lines.append(str(exc))
                verbose_renderer.update(state, lines)

            if now - last_progress >= PROGRESS_INTERVAL:
                # TTY 已有转圈/滚动区域，只在非 TTY 追加心跳
                if (
                    not verbose_renderer.is_tty
                    and state not in ("done", "error")
                ):
                    progress.update(
                        f"Cloud-init status: {state} "
                        f"({int(now - started)}s elapsed)"
                    )
                last_progress = now

            if state == "done":
                # 成功时不保留 verbose 区域，清屏后只输出最终结果
                verbose_renderer.clear()
                progress.clear()
                elapsed = int(time.monotonic() - started)
                print(f"Cloud-init completed successfully in {elapsed}s.")
                return 0
            if state == "error":
                verbose_renderer.clear()
                progress.clear()
                render._print_failure(name, target)
                return 1

            if verbose:
                time.sleep(VERBOSE_INTERVAL)
            elif progress.is_tty:
                progress.animate(POLL_INTERVAL, state, started)
            else:
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        verbose_renderer.clear()
        progress.clear()
        print(
            f"Stopped waiting for cloud-init. Instance {name} keeps running."
        )
        return 0
