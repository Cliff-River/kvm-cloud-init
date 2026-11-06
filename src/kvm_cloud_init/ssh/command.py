"""一次性远端命令：捕获输出与流式跟随。

两者都不分配 PTY，输出直接写入本地 stdout/stderr。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import client


def capture_command(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    command: str,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> tuple[str, str, int]:
    """在远端执行命令并捕获输出，返回 ``(stdout, stderr, exit_code)``。"""
    ssh_client = client._connect(instance, host, user, key_path, deadline=deadline)
    try:
        _stdin, stdout, stderr = ssh_client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        ssh_client.close()


def stream_command(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    command: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> int:
    """流式执行远端命令，直到通道结束或 ``should_stop()`` 返回 True。

    用于 ``cloud-init log -f``：输出实时转发到本地 stdout/stderr；
    被 ``should_stop`` 提前终止时返回 0，正常结束时返回远端退出码。
    """
    ssh_client = client._connect(instance, host, user, key_path, deadline=deadline)
    try:
        _stdin, stdout, _stderr = ssh_client.exec_command(command)
        chan = stdout.channel
        chan.setblocking(0)
        while True:
            moved = False
            if chan.recv_ready():
                sys.stdout.buffer.write(chan.recv(client._BUFSIZE))
                sys.stdout.buffer.flush()
                moved = True
            if chan.recv_stderr_ready():
                sys.stderr.buffer.write(chan.recv_stderr(client._BUFSIZE))
                sys.stderr.buffer.flush()
                moved = True
            if should_stop is not None and should_stop():
                chan.close()
                return 0
            if (
                chan.exit_status_ready()
                and not chan.recv_ready()
                and not chan.recv_stderr_ready()
            ):
                return chan.recv_exit_status()
            if not moved:
                time.sleep(0.2)
    finally:
        ssh_client.close()
