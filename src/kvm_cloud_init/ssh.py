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
from pathlib import Path

import paramiko

from .config import project_root
from .errors import SshError

#: 连接超时（秒）
CONNECT_TIMEOUT = 10
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
            f"实例 {instance} 的公钥不存在：{pub}；请先执行 kvmci create <实例名>"
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


def _connect(instance: str, host: str, user: str, key_path: Path) -> paramiko.SSHClient:
    """建立 SSH 连接；主机密钥自动接受（适用于本地临时实例）。"""
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
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise SshError(
            f"无法连接实例 {instance}（{user}@{host}）：{exc}"
        ) from exc
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


def interactive_shell(instance: str, host: str, user: str, key_path: Path) -> int:
    """打开交互式登录会话，返回远端退出码。"""
    client = _connect(instance, host, user, key_path)
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
        chan.setblocking(0)
        stdin_fd = sys.stdin.fileno()
        old_attrs = None
        if sys.stdin.isatty():
            old_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
            signal.signal(signal.SIGWINCH, lambda *_: _sync_window_size(chan))
        _sync_window_size(chan)
        try:
            return _pump(chan, stdin_fd)
        finally:
            if old_attrs is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
    finally:
        client.close()


def _pump(chan: paramiko.Channel, stdin_fd: int) -> int:
    """在本地 stdin 与远端 channel 之间双向转发，直到远端结束。"""
    while True:
        if chan.exit_status_ready() and not chan.recv_ready():
            break
        readable, _, _ = select.select([stdin_fd, chan], [], [], 0.5)
        if chan in readable:
            data = chan.recv(_BUFSIZE)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        if stdin_fd in readable:
            data = os.read(stdin_fd, _BUFSIZE)
            if data:
                chan.sendall(data)
            else:
                chan.shutdown_write()
    return chan.recv_exit_status()


def run_command(instance: str, host: str, user: str, key_path: Path, command: str) -> int:
    """在远端执行一条命令，透传输出，返回远端退出码。"""
    client = _connect(instance, host, user, key_path)
    try:
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
