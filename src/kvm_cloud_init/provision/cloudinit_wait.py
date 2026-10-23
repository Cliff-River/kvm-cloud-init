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
    conn: libvirt.virConnect, name: str, deadline: float
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
            time.sleep(POLL_INTERVAL)


class _VerboseRenderer:
    """固定 7 行区域滚动显示 cloud-init 输出。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._is_tty = bool(getattr(stream, "isatty", lambda: False)())
        self._rendered = 0
        self._seen: set[str] = set()

    def update(self, status: str, lines: list[str]) -> None:
        lines = lines[-VERBOSE_LINES:]
        if not self._is_tty:
            for line in lines:
                if line not in self._seen:
                    self._seen.add(line)
                    print(line)
            return

        rows = [f"cloud-init: {status}", *lines]
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
    target: tuple[str, str, Path] | None = None
    state = "not started"
    last_check = 0.0

    try:
        while True:
            now = time.monotonic()
            if target is None:
                target = _wait_target(conn, name, deadline)

            if now - last_check >= POLL_INTERVAL:
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
                    time.sleep(POLL_INTERVAL)
                    continue
                last_check = now

            if verbose:
                try:
                    lines = _tail_lines(name, target, OUTPUT_LOG, VERBOSE_LINES)
                except (ssh.SshError, CloudInitError):
                    lines = []
                renderer.update(state, lines)

            if state == "done":
                renderer.clear()
                elapsed = int(time.monotonic() - started)
                print(f"Cloud-init completed successfully in {elapsed}s.")
                return 0
            if state == "error":
                renderer.clear()
                _print_failure(name, target)
                return 1

            time.sleep(VERBOSE_INTERVAL if verbose else POLL_INTERVAL)
    except KeyboardInterrupt:
        renderer.clear()
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
