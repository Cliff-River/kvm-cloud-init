"""交互式 shell 与一次性命令的终端转发。

本地 stdin/stdout 均为终端时申请 PTY 并双向搬运字节，否则只排空输出。
``port_forwards`` 在 shell 存活期间复用同一 SSH Transport 建立本地转发。
"""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

import paramiko

from . import client
from .portforward import PortForwarder, PortMapping


def _sync_window_size(chan: paramiko.Channel) -> None:
    try:
        size = os.get_terminal_size()
    except OSError:
        return
    try:
        chan.resize_pty(width=size.columns, height=size.lines)
    except paramiko.SSHException:
        pass


def interactive_shell(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    *,
    deadline: float | None = None,
    on_retry: Callable[[int, float], None] | None = None,
    port_forwards: Sequence[PortMapping] | None = None,
) -> int:
    """打开交互式登录会话，返回远端退出码。

    ``port_forwards`` 为 ``(本地端口, 虚拟机端口)`` 序列时，会在 shell 存活
    期间于同一 SSH Transport 上建立本地端口转发，退出时统一关闭。
    """
    ssh_client = client._connect(
        instance, host, user, key_path, deadline=deadline, on_retry=on_retry
    )
    forwarder: PortForwarder | None = None
    try:
        if port_forwards:
            # 在 invoke_shell 之前绑定：本地端口被占用时立即失败，
            # 而不是进入交互会话后才让用户发现转发不可用。
            forwarder = PortForwarder(ssh_client.get_transport(), port_forwards)
            forwarder.start()
        # 透传本地 TERM 与窗口尺寸：paramiko 默认 term='vt100'，
        # 会导致远端程序误判终端不支持 256 色，输出全部退化为默认（灰）色。
        term = os.environ.get("TERM", "xterm-256color")
        try:
            size = os.get_terminal_size()
            width, height = size.columns, size.lines
        except OSError:
            width, height = 80, 24
        chan = ssh_client.invoke_shell(term=term, width=width, height=height)
        return _pump_interactive(chan, sys.stdin.fileno())
    finally:
        if forwarder is not None:
            forwarder.stop()
        ssh_client.close()


def _pump_interactive(chan: paramiko.Channel, stdin_fd: int) -> int:
    """在本地终端与远端 channel 之间双向转发，返回远端退出码。"""
    chan.setblocking(0)
    old_attrs = None
    # 记录并在退出时恢复原 SIGWINCH handler，避免污染同一进程内的后续操作。
    old_winch = signal.getsignal(signal.SIGWINCH)
    try:
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)

            def _on_winch(_signum: int, _frame: FrameType | None) -> None:
                _sync_window_size(chan)

            signal.signal(signal.SIGWINCH, _on_winch)
        _sync_window_size(chan)
        return _pump(chan, stdin_fd)
    finally:
        if old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_winch)


def _pump(chan: paramiko.Channel, stdin_fd: int | None) -> int:
    """在本地 stdin 与远端 channel 之间双向转发，直到远端结束。"""
    while True:
        if chan.exit_status_ready() and not chan.recv_ready():
            break
        # select 的 watch 列表只放 int 型 fd：select 对 Channel 也是调用
        # fileno()，混合 int 与对象会让类型检查器无法判定成员关系；
        # 通道是否有数据改用 recv_ready()（paramiko 内部也可能缓冲了
        # select 信号之外的已解密数据）。
        watch: list[int] = [chan.fileno()]
        if stdin_fd is not None:
            watch.append(stdin_fd)
        readable, _, _ = select.select(watch, [], [], 0.5)
        if chan.recv_ready():
            data = chan.recv(client._BUFSIZE)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        if stdin_fd is not None and stdin_fd in readable:
            data = os.read(stdin_fd, client._BUFSIZE)
            if data:
                # 通道处于非阻塞模式，发送时临时切回阻塞，避免短写自旋。
                chan.settimeout(None)
                chan.sendall(data)
                chan.settimeout(0.0)
            else:
                chan.shutdown_write()
                stdin_fd = None
    return chan.recv_exit_status()


def _stdio_is_tty() -> bool:
    """本地 stdin 与 stdout 是否同时是终端。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_command(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    command: str,
    *,
    deadline: float | None = None,
    on_retry: Callable[[int, float], None] | None = None,
) -> int:
    """在远端执行一条命令，透传输出，返回远端退出码。

    本地 stdin/stdout 都是终端时（如 ``exec <实例> -- bash``）申请 PTY
    并双向转发，使远端交互式程序可正常使用；重定向或管道场景保持
    不分配 PTY，避免 CRLF 与终端格式污染。
    """
    ssh_client = client._connect(
        instance, host, user, key_path, deadline=deadline, on_retry=on_retry
    )
    try:
        if _stdio_is_tty():
            # 必须保留 stdin/stderr 文件对象：其析构会调用 shutdown_write，
            # 导致通道立即进入 EOF、后续输入无法发送。
            _stdin, stdout, _stderr = ssh_client.exec_command(command, get_pty=True)
            chan = stdout.channel
            return _pump_interactive(chan, sys.stdin.fileno())
        _stdin, stdout, _stderr = ssh_client.exec_command(command)
        chan = stdout.channel
        chan.setblocking(0)
        _drain(chan)
        return chan.recv_exit_status()
    finally:
        ssh_client.close()


def _drain(chan: paramiko.Channel) -> None:
    """非阻塞读取 stdout/stderr 直到通道关闭。"""
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
        if (
            chan.exit_status_ready()
            and not chan.recv_ready()
            and not chan.recv_stderr_ready()
        ):
            return
        if not moved:
            time.sleep(0.02)
