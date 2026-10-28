"""SSH 密钥管理与连接。

为每个实例维护一对密钥（``keys/<实例名>`` 与 ``keys/<实例名>.pub``），
并用它建立到虚拟机的交互式 shell 或一次性命令执行。全部走 paramiko，
不新增 subprocess 调用 ``ssh`` / ``ssh-keygen``。
"""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import paramiko

from .config import project_root
from .errors import SshError

#: 单次连接超时（秒）
CONNECT_TIMEOUT = 10
#: 连接失败后的重试间隔（秒）
CONNECT_RETRY_INTERVAL = 2.0
#: 连接失败后的重试总时长（秒）；调用方可用 deadline 覆盖
CONNECT_RETRY_TIMEOUT = 60.0
#: 读写缓冲区
_BUFSIZE = 65536


def keys_dir() -> Path:
    """密钥目录：项目根下的 ``keys/``（已在 .gitignore 中）。"""
    return project_root() / "keys"


def private_key_path(instance: str) -> Path:
    return keys_dir() / instance


def public_key_path(instance: str) -> Path:
    return keys_dir() / f"{instance}.pub"


def _generate_key() -> paramiko.PKey:
    """生成密钥对：首选 Ed25519，运行环境不支持时回退 RSA-3072。"""
    generator = getattr(paramiko.Ed25519Key, "generate", None)
    if generator is not None:
        return generator()
    return paramiko.RSAKey.generate(3072)


def ensure_keypair(instance: str) -> Path:
    """确保 ``keys/<实例名>`` 与 ``.pub`` 存在，返回私钥路径。

    幂等：密钥对已存在则复用，不覆盖、不重新生成。
    """
    priv = private_key_path(instance)
    pub = public_key_path(instance)
    if priv.is_file() and pub.is_file():
        return priv

    key = _generate_key()
    keys_dir().mkdir(parents=True, exist_ok=True)
    key.write_private_key_file(str(priv))
    os.chmod(priv, 0o600)
    pub.write_text(f"{key.get_name()} {key.get_base64()}\n", encoding="utf-8")
    os.chmod(pub, 0o644)
    return priv


def read_public_key(instance: str) -> str:
    """读取公钥文本（单行 OpenSSH 格式）。"""
    pub = public_key_path(instance)
    if not pub.is_file():
        raise SshError(
            f"Public key for instance {instance} does not exist: {pub}; "
            "run 'kvmci create <name>' first"
        )
    return pub.read_text(encoding="utf-8").strip()


def delete_keypair(instance: str) -> list[Path]:
    """删除程序生成的密钥对，返回实际删除的文件列表。

    仅处理 R1 命名约定的两个文件；缺失即跳过，不报错。
    """
    removed: list[Path] = []
    for path in (private_key_path(instance), public_key_path(instance)):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def _connect(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    *,
    deadline: float | None = None,
    on_retry: Callable[[int, float], None] | None = None,
) -> paramiko.SSHClient:
    """建立 SSH 连接；主机密钥自动接受（适用于本地临时实例）。

    实例刚启动时 IP 往往先于 sshd 就绪，连接失败按 ``CONNECT_RETRY_INTERVAL``
    重试直到 ``deadline``（``time.monotonic()`` 绝对时间，缺省为当前时间 +
    ``CONNECT_RETRY_TIMEOUT``）；认证失败不重试，重试不会改变结果。
    """
    if deadline is None:
        deadline = time.monotonic() + CONNECT_RETRY_TIMEOUT
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=user,
                key_filename=str(key_path),
                timeout=CONNECT_TIMEOUT,
                banner_timeout=CONNECT_TIMEOUT,
                auth_timeout=CONNECT_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SshError(
                f"Cannot connect to instance {instance} ({user}@{host}): {exc}"
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            now = time.monotonic()
            if now >= deadline:
                waited = int(now - started)
                suffix = f" after {waited}s" if waited >= 1 else ""
                raise SshError(
                    f"Cannot connect to instance {instance} ({user}@{host})"
                    f"{suffix}: {exc}"
                ) from exc
            delay = min(CONNECT_RETRY_INTERVAL, deadline - now)
            if on_retry is not None:
                on_retry(attempt, delay)
            time.sleep(delay)
        else:
            return client


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
) -> int:
    """打开交互式登录会话，返回远端退出码。"""
    client = _connect(
        instance, host, user, key_path, deadline=deadline, on_retry=on_retry
    )
    try:
        # 透传本地 TERM 与窗口尺寸：paramiko 默认 term='vt100'，
        # 会导致远端程序误判终端不支持 256 色，输出全部退化为默认（灰）色。
        term = os.environ.get("TERM", "xterm-256color")
        try:
            size = os.get_terminal_size()
            width, height = size.columns, size.lines
        except OSError:
            width, height = 80, 24
        chan = client.invoke_shell(term=term, width=width, height=height)
        return _pump_interactive(chan, sys.stdin.fileno())
    finally:
        client.close()


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
            data = chan.recv(_BUFSIZE)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        if stdin_fd is not None and stdin_fd in readable:
            data = os.read(stdin_fd, _BUFSIZE)
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
    client = _connect(
        instance, host, user, key_path, deadline=deadline, on_retry=on_retry
    )
    try:
        if _stdio_is_tty():
            # 必须保留 stdin/stderr 文件对象：其析构会调用 shutdown_write，
            # 导致通道立即进入 EOF、后续输入无法发送。
            _stdin, stdout, _stderr = client.exec_command(command, get_pty=True)
            chan = stdout.channel
            return _pump_interactive(chan, sys.stdin.fileno())
        _stdin, stdout, _stderr = client.exec_command(command)
        chan = stdout.channel
        chan.setblocking(0)
        _drain(chan)
        return chan.recv_exit_status()
    finally:
        client.close()


def _drain(chan: paramiko.Channel) -> None:
    """非阻塞读取 stdout/stderr 直到通道关闭。"""
    while True:
        moved = False
        if chan.recv_ready():
            sys.stdout.buffer.write(chan.recv(_BUFSIZE))
            sys.stdout.buffer.flush()
            moved = True
        if chan.recv_stderr_ready():
            sys.stderr.buffer.write(chan.recv_stderr(_BUFSIZE))
            sys.stderr.buffer.flush()
            moved = True
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            return
        if not moved:
            time.sleep(0.02)


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
    client = _connect(instance, host, user, key_path, deadline=deadline)
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        client.close()


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
    client = _connect(instance, host, user, key_path, deadline=deadline)
    try:
        _stdin, stdout, _stderr = client.exec_command(command)
        chan = stdout.channel
        chan.setblocking(0)
        while True:
            moved = False
            if chan.recv_ready():
                sys.stdout.buffer.write(chan.recv(_BUFSIZE))
                sys.stdout.buffer.flush()
                moved = True
            if chan.recv_stderr_ready():
                sys.stderr.buffer.write(chan.recv_stderr(_BUFSIZE))
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
        client.close()
