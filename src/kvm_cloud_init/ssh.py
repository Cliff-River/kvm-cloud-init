"""SSH 密钥管理与连接。

为每个实例维护一对密钥（``keys/<实例名>`` 与 ``keys/<实例名>.pub``），
并用它建立到虚拟机的交互式 shell 或一次性命令执行。全部走 paramiko，
不新增 subprocess 调用 ``ssh`` / ``ssh-keygen``。
"""

from __future__ import annotations

import os
import posixpath
import select
import signal
import stat
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

import paramiko

from .config import project_root
from .errors import SshError
from .portforward import PortForwarder, PortMapping

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
    port_forwards: Sequence[PortMapping] | None = None,
) -> int:
    """打开交互式登录会话，返回远端退出码。

    ``port_forwards`` 为 ``(本地端口, 虚拟机端口)`` 序列时，会在 shell 存活
    期间于同一 SSH Transport 上建立本地端口转发，退出时统一关闭。
    """
    client = _connect(
        instance, host, user, key_path, deadline=deadline, on_retry=on_retry
    )
    forwarder: PortForwarder | None = None
    try:
        if port_forwards:
            # 在 invoke_shell 之前绑定：本地端口被占用时立即失败，
            # 而不是进入交互会话后才让用户发现转发不可用。
            forwarder = PortForwarder(client.get_transport(), port_forwards)
            forwarder.start()
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
        if forwarder is not None:
            forwarder.stop()
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


# ---- SFTP 文件传输（cp 子命令；不调用 scp/sftp/ssh 子进程） ----


def _sftp_reason(exc: OSError) -> str:
    """把 SFTP 异常翻译成具体原因，避免直接展示 paramiko/OpenSSH 的裸 ``Failure``。"""
    if isinstance(exc, FileNotFoundError):
        return "no such file or directory"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, IsADirectoryError):
        return "is a directory"
    if isinstance(exc, NotADirectoryError):
        return "not a directory"
    # 部分 paramiko 版本/绑定会在 OSError 上附带 SSH_FX_* code；补充常见码位
    code = getattr(exc, "code", None)
    code_reasons = {
        4: "the SFTP server rejected the operation",
        5: "bad SFTP message",
        6: "no SFTP connection",
        7: "SFTP connection lost",
        8: "operation not supported by the SFTP server",
        11: "no space left on the filesystem",
        12: "quota exceeded",
        14: "the path is not a directory",
        18: "read-only filesystem",
        21: "the path is a directory",
    }
    if code is not None:
        return code_reasons.get(code, f"SFTP failure (code {code})")
    # paramiko 5.x 对 SSH_FX_FAILURE 只抛 OSError("Failure")，
    # 这是 OpenSSH 对“把目录当文件打开/无权访问/路径异常”等情况的通用回答
    text = str(exc) or (exc.strerror or "")
    if text in ("", "Failure"):
        return (
            "the SFTP server rejected the operation (the path may be missing, "
            "may be a directory, or access may be denied)"
        )
    return text


def _local_reason(exc: OSError) -> str:
    """本地文件系统异常的可读原因。"""
    detail = exc.strerror or str(exc) or exc.__class__.__name__
    filename = f": {exc.filename}" if getattr(exc, "filename", None) else ""
    return f"{detail}{filename}"


def remote_is_directory(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    remote_path: str,
    *,
    deadline: float | None = None,
) -> bool:
    """远端路径是否为目录；路径不存在返回 False，其他 SFTP 错误抛 SshError。"""
    client = _connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = client.open_sftp()
        try:
            try:
                attrs = sftp.stat(remote_path)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise SshError(
                    f"Cannot stat remote path {instance}:{remote_path}: "
                    f"{_sftp_reason(exc)}"
                ) from exc
            return stat.S_ISDIR(attrs.st_mode or 0)
        finally:
            sftp.close()
    finally:
        client.close()


def _ensure_remote_dirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """``mkdir -p``：逐级创建远端路径中缺失的目录。

    绝对路径从根开始；相对路径相对于 SFTP 默认目录（通常是登录用户的家
    目录）逐级解析，与 scp/sftp 的相对路径语义一致。
    """
    current = "/" if remote_dir.startswith("/") else ""
    for part in remote_dir.split("/"):
        if not part or part == ".":
            continue
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            try:
                sftp.mkdir(current)
            except FileNotFoundError as exc:
                raise SshError(
                    f"Cannot create remote directory {current}: "
                    "no such file or directory"
                ) from exc
            except OSError as exc:
                raise SshError(
                    f"Cannot create remote directory {current}: {_sftp_reason(exc)}"
                ) from exc
        except OSError as exc:
            raise SshError(
                f"Cannot access remote directory {current}: {_sftp_reason(exc)}"
            ) from exc


def _remote_kind(
    sftp: paramiko.SFTPClient, instance: str, remote_path: str
) -> int:
    """lstat 远端路径并返回 S_IF* 类型位；路径不存在抛带具体原因的 SshError。"""
    try:
        attrs = sftp.lstat(remote_path)
    except FileNotFoundError as exc:
        raise SshError(
            f"Remote path not found: {instance}:{remote_path}"
        ) from exc
    except OSError as exc:
        raise SshError(
            f"Cannot stat remote path {instance}:{remote_path}: {_sftp_reason(exc)}"
        ) from exc
    return stat.S_IFMT(attrs.st_mode or 0)


def _download_tree(
    sftp: paramiko.SFTPClient,
    instance: str,
    remote_dir: str,
    local_dir: Path,
    counts: list[int],
) -> None:
    """递归下载远端目录到本地（保留符号链接）。"""
    try:
        local_dir.mkdir(parents=False, exist_ok=True)
    except FileNotFoundError as exc:
        raise SshError(
            f"Cannot create local directory {local_dir}: no such file or directory"
        ) from exc
    except NotADirectoryError as exc:
        raise SshError(f"Local path is not a directory: {local_dir}") from exc
    except OSError as exc:
        raise SshError(
            f"Cannot create local directory {local_dir}: {_local_reason(exc)}"
        ) from exc

    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError as exc:
        raise SshError(
            f"Cannot list remote directory {instance}:{remote_dir}: "
            f"{_sftp_reason(exc)}"
        ) from exc

    for entry in sorted(entries, key=lambda item: item.filename):
        remote_child = f"{remote_dir.rstrip('/')}/{entry.filename}"
        local_child = local_dir / entry.filename
        mode = stat.S_IFMT(entry.st_mode or 0)
        try:
            if stat.S_ISLNK(mode):
                link_target = sftp.readlink(remote_child)
                if local_child.is_symlink() or local_child.exists():
                    local_child.unlink()
                os.symlink(link_target, local_child)
            elif stat.S_ISDIR(mode):
                _download_tree(sftp, instance, remote_child, local_child, counts)
            elif stat.S_ISREG(mode):
                sftp.get(remote_child, str(local_child))
                counts[0] += 1
            else:
                raise SshError(
                    f"Unsupported remote file type at {instance}:{remote_child} "
                    f"(mode {oct(mode)})"
                )
        except SshError:
            raise
        except OSError as exc:
            raise SshError(
                f"Cannot download {instance}:{remote_child} to {local_child}: "
                f"{_sftp_reason(exc)}"
            ) from exc


def _upload_tree(
    sftp: paramiko.SFTPClient,
    instance: str,
    local_dir: Path,
    remote_dir: str,
    counts: list[int],
) -> None:
    """递归上传本地目录到远端（保留符号链接）。"""
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        _ensure_remote_dirs(sftp, remote_dir)
    except OSError as exc:
        raise SshError(
            f"Cannot access remote directory {instance}:{remote_dir}: "
            f"{_sftp_reason(exc)}"
        ) from exc

    try:
        children = sorted(os.scandir(local_dir), key=lambda item: item.name)
    except OSError as exc:
        raise SshError(
            f"Cannot list local directory {local_dir}: {_local_reason(exc)}"
        ) from exc

    for entry in children:
        local_child = Path(entry.path)
        remote_child = f"{remote_dir.rstrip('/')}/{entry.name}"
        try:
            if entry.is_symlink():
                link_target = os.readlink(local_child)
                try:
                    sftp.remove(remote_child)
                except OSError:
                    pass
                sftp.symlink(link_target, remote_child)
            elif entry.is_dir(follow_symlinks=False):
                _upload_tree(sftp, instance, local_child, remote_child, counts)
            elif entry.is_file(follow_symlinks=False):
                sftp.put(str(local_child), remote_child)
                counts[0] += 1
            else:
                raise SshError(
                    f"Unsupported local file type: {local_child} "
                    "(only regular files, directories and symlinks are supported)"
                )
        except SshError:
            raise
        except OSError as exc:
            raise SshError(
                f"Cannot upload {local_child} to {instance}:{remote_child}: "
                f"{_sftp_reason(exc)}"
            ) from exc


def upload_path(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    local_path: Path,
    remote_path: str,
    *,
    deadline: float | None = None,
) -> int:
    """上传本地文件或目录（递归）到 ``remote_path``，返回传输的普通文件数。"""
    if not local_path.exists() and not local_path.is_symlink():
        raise SshError(f"Local path not found: {local_path}")
    client = _connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = client.open_sftp()
        counts = [0]
        try:
            if local_path.is_symlink():
                link_target = os.readlink(local_path)
                sftp.symlink(link_target, remote_path)
            elif local_path.is_dir():
                _upload_tree(sftp, instance, local_path, remote_path, counts)
            elif local_path.is_file():
                parent = posixpath.dirname(remote_path.rstrip("/"))
                if parent and parent != "/":
                    _ensure_remote_dirs(sftp, parent)
                try:
                    sftp.put(str(local_path), remote_path)
                except OSError as exc:
                    raise SshError(
                        f"Cannot upload {local_path} to {instance}:{remote_path}: "
                        f"{_sftp_reason(exc)}"
                    ) from exc
                counts[0] += 1
            else:
                raise SshError(
                    f"Unsupported local file type: {local_path} "
                    "(only regular files, directories and symlinks are supported)"
                )
        except SshError:
            raise
        except (OSError, paramiko.SSHException) as exc:
            raise SshError(
                f"Cannot upload {local_path} to {instance}:{remote_path}: "
                f"{_sftp_reason(exc) if isinstance(exc, OSError) else exc}"
            ) from exc
        finally:
            sftp.close()
        return counts[0]
    finally:
        client.close()


def download_path(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    remote_path: str,
    local_path: Path,
    *,
    deadline: float | None = None,
) -> int:
    """下载远端文件或目录（递归）到 ``local_path``，返回传输的普通文件数。"""
    client = _connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = client.open_sftp()
        counts = [0]
        try:
            kind = _remote_kind(sftp, instance, remote_path)
            if stat.S_ISLNK(kind):
                link_target = sftp.readlink(remote_path)
                if local_path.is_symlink() or local_path.exists():
                    local_path.unlink()
                os.symlink(link_target, local_path)
            elif stat.S_ISDIR(kind):
                parent = local_path.parent
                if not parent.is_dir():
                    raise SshError(
                        f"Local directory does not exist: {parent}"
                    )
                _download_tree(sftp, instance, remote_path, local_path, counts)
            elif stat.S_ISREG(kind):
                parent = local_path.parent
                if not parent.is_dir():
                    raise SshError(
                        f"Local directory does not exist: {parent}"
                    )
                try:
                    sftp.get(remote_path, str(local_path))
                except OSError as exc:
                    raise SshError(
                        f"Cannot download {instance}:{remote_path} to "
                        f"{local_path}: {_sftp_reason(exc)}"
                    ) from exc
                counts[0] += 1
            else:
                raise SshError(
                    f"Unsupported remote file type at {instance}:{remote_path} "
                    f"(mode {oct(kind)})"
                )
        finally:
            sftp.close()
        return counts[0]
    finally:
        client.close()


# ---- SFTP 镜像同步（sync 子命令；rclone sync 语义：目标成为源的镜像） ----


@dataclass
class SyncStats:
    """一次 sync 的变更计数。"""

    files_transferred: int = 0
    dirs_created: int = 0
    symlinks_created: int = 0
    deleted_entries: int = 0
    deleted: list[str] = field(default_factory=list)


def _guard_remote_sync_target(remote_path: str) -> None:
    """同步会删除目标中的多余内容，远端根目录一律拒绝。"""
    if remote_path == "/":
        raise SshError("Refusing to sync to remote path '/'")


def _guard_local_sync_target(local_path: Path) -> None:
    """同步会删除目标中的多余内容，本地根目录一律拒绝。"""
    if local_path == Path("/"):
        raise SshError("Refusing to sync to local path '/'")


def _remote_list(
    sftp: paramiko.SFTPClient, instance: str, remote_dir: str
) -> dict[str, paramiko.SFTPAttributes]:
    """lstat 列出远端目录条目（不跟随符号链接）；目录不存在抛 SshError。"""
    try:
        entries = sftp.listdir_attr(remote_dir)
    except FileNotFoundError as exc:
        raise SshError(
            f"Remote path not found: {instance}:{remote_dir}"
        ) from exc
    except OSError as exc:
        raise SshError(
            f"Cannot list remote directory {instance}:{remote_dir}: "
            f"{_sftp_reason(exc)}"
        ) from exc
    return {entry.filename: entry for entry in entries}


def _remove_remote_tree(
    sftp: paramiko.SFTPClient,
    instance: str,
    remote_path: str,
    mode: int,
    stats: SyncStats,
) -> None:
    """递归删除远端目录树或删除单个文件/符号链接。"""
    try:
        if stat.S_ISDIR(mode):
            for entry in sftp.listdir_attr(remote_path):
                child = f"{remote_path.rstrip('/')}/{entry.filename}"
                _remove_remote_tree(
                    sftp, instance, child, stat.S_IFMT(entry.st_mode or 0), stats
                )
            sftp.rmdir(remote_path)
        else:
            sftp.remove(remote_path)
        stats.deleted_entries += 1
        stats.deleted.append(f"{instance}:{remote_path}")
    except OSError as exc:
        raise SshError(
            f"Cannot delete {instance}:{remote_path}: {_sftp_reason(exc)}"
        ) from exc


def _remove_local_tree(path: Path, stats: SyncStats) -> None:
    """递归删除本地目录树或删除单个文件/符号链接。"""
    try:
        if path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                _remove_local_tree(child, stats)
            path.rmdir()
        else:
            path.unlink()
        stats.deleted_entries += 1
        stats.deleted.append(str(path))
    except OSError as exc:
        raise SshError(f"Cannot delete local path {path}: {_local_reason(exc)}") from exc


def _sync_dir_upload(
    sftp: paramiko.SFTPClient,
    instance: str,
    src_dir: Path,
    dst_dir: str,
    stats: SyncStats,
) -> None:
    """把本地 ``src_dir`` 的内容镜像到远端 ``dst_dir``（双向类型替换）。"""
    try:
        source_entries = {
            entry.name: entry for entry in os.scandir(src_dir)
        }
    except OSError as exc:
        raise SshError(
            f"Cannot list local directory {src_dir}: {_local_reason(exc)}"
        ) from exc

    dest_entries = _remote_list(sftp, instance, dst_dir)

    for name, src_entry in source_entries.items():
        src_path = Path(src_entry.path)
        dst_path = f"{dst_dir.rstrip('/')}/{name}"
        dst_attrs = dest_entries.pop(name, None)
        dst_kind = (
            stat.S_IFMT(dst_attrs.st_mode or 0) if dst_attrs is not None else None
        )
        try:
            if src_entry.is_symlink():
                link_target = os.readlink(src_path)
                same_link = (
                    dst_kind is not None
                    and stat.S_ISLNK(dst_kind)
                    and _safe_readlink(sftp, dst_path) == link_target
                )
                if not same_link:
                    if dst_attrs is not None:
                        _remove_remote_tree(sftp, instance, dst_path, dst_kind, stats)  # type: ignore[arg-type]
                    sftp.symlink(link_target, dst_path)
                    stats.symlinks_created += 1
            elif src_entry.is_dir(follow_symlinks=False):
                if dst_kind is not None and not stat.S_ISDIR(dst_kind):
                    _remove_remote_tree(sftp, instance, dst_path, dst_kind, stats)  # type: ignore[arg-type]
                    dst_kind = None
                if dst_kind is None:
                    _ensure_remote_dirs(sftp, dst_path)
                    stats.dirs_created += 1
                _sync_dir_upload(sftp, instance, src_path, dst_path, stats)
            elif src_entry.is_file(follow_symlinks=False):
                src_stat = src_entry.stat(follow_symlinks=False)
                outdated = (
                    dst_kind is None
                    or not stat.S_ISREG(dst_kind)
                    or dst_attrs.st_size != src_stat.st_size
                    or int(dst_attrs.st_mtime or 0) != int(src_stat.st_mtime)
                )
                if dst_kind is not None and not stat.S_ISREG(dst_kind):
                    _remove_remote_tree(sftp, instance, dst_path, dst_kind, stats)  # type: ignore[arg-type]
                    outdated = True
                if outdated:
                    sftp.put(str(src_path), dst_path)
                    # 保留源 mtime，使下一次 sync 可凭 size+mtime 跳过
                    sftp.utime(dst_path, (src_stat.st_mtime, src_stat.st_mtime))
                    stats.files_transferred += 1
            else:
                raise SshError(
                    f"Unsupported local file type: {src_path} "
                    "(only regular files, directories and symlinks are supported)"
                )
        except SshError:
            raise
        except OSError as exc:
            raise SshError(
                f"Cannot sync {src_path} to {instance}:{dst_path}: "
                f"{_sftp_reason(exc)}"
            ) from exc

    # 目标中剩余条目在源中已不存在：递归删除
    for name, dst_attrs in dest_entries.items():
        _remove_remote_tree(
            sftp,
            instance,
            f"{dst_dir.rstrip('/')}/{name}",
            stat.S_IFMT(dst_attrs.st_mode or 0),
            stats,
        )


def _safe_readlink(sftp: paramiko.SFTPClient, remote_path: str) -> str | None:
    try:
        return sftp.readlink(remote_path)
    except OSError:
        return None


def _sync_dir_download(
    sftp: paramiko.SFTPClient,
    instance: str,
    src_dir: str,
    dst_dir: Path,
    stats: SyncStats,
) -> None:
    """把远端 ``src_dir`` 的内容镜像到本地 ``dst_dir``（双向类型替换）。"""
    source_entries = _remote_list(sftp, instance, src_dir)

    try:
        dest_names = set(os.listdir(dst_dir))
    except FileNotFoundError as exc:
        raise SshError(f"Local path not found: {dst_dir}") from exc
    except OSError as exc:
        raise SshError(
            f"Cannot list local directory {dst_dir}: {_local_reason(exc)}"
        ) from exc

    for name, src_attrs in sorted(source_entries.items()):
        src_path = f"{src_dir.rstrip('/')}/{name}"
        dst_path = dst_dir / name
        dest_names.discard(name)
        src_kind = stat.S_IFMT(src_attrs.st_mode or 0)
        try:
            if stat.S_ISLNK(src_kind):
                link_target = sftp.readlink(src_path)
                same_link = (
                    dst_path.is_symlink() and os.readlink(dst_path) == link_target
                )
                if not same_link:
                    if dst_path.is_symlink() or dst_path.exists():
                        _remove_local_tree(dst_path, stats)
                    os.symlink(link_target, dst_path)
                    stats.symlinks_created += 1
            elif stat.S_ISDIR(src_kind):
                if dst_path.is_symlink() or (
                    dst_path.exists() and not dst_path.is_dir()
                ):
                    _remove_local_tree(dst_path, stats)
                if not dst_path.exists():
                    dst_path.mkdir()
                    stats.dirs_created += 1
                _sync_dir_download(sftp, instance, src_path, dst_path, stats)
            elif stat.S_ISREG(src_kind):
                outdated = True
                if dst_path.is_file():
                    dst_stat = dst_path.stat()
                    outdated = (
                        dst_stat.st_size != src_attrs.st_size
                        or int(dst_stat.st_mtime) != int(src_attrs.st_mtime or 0)
                    )
                elif dst_path.is_symlink() or dst_path.exists():
                    _remove_local_tree(dst_path, stats)
                if outdated:
                    sftp.get(src_path, str(dst_path))
                    os.utime(
                        dst_path,
                        (src_attrs.st_mtime or 0, src_attrs.st_mtime or 0),
                    )
                    stats.files_transferred += 1
            else:
                raise SshError(
                    f"Unsupported remote file type at {instance}:{src_path} "
                    f"(mode {oct(src_kind)})"
                )
        except SshError:
            raise
        except OSError as exc:
            raise SshError(
                f"Cannot sync {instance}:{src_path} to {dst_path}: "
                f"{_sftp_reason(exc)}"
            ) from exc

    for name in sorted(dest_names):
        _remove_local_tree(dst_dir / name, stats)


def sync_to_remote(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    local_dir: Path,
    remote_path: str,
    *,
    deadline: float | None = None,
) -> SyncStats:
    """把本地目录镜像到远端（rclone sync 语义：删除远端多余条目）。

    ``remote_path`` 可以是绝对路径，也可以是相对于 SFTP 默认目录（登录用户
    家目录）的相对路径，与 ``cp`` / scp 一致。
    """
    if not local_dir.is_dir():
        raise SshError(f"Local source directory not found: {local_dir}")
    _guard_remote_sync_target(remote_path)
    client = _connect(instance, host, user, key_path, deadline=deadline)
    stats = SyncStats()
    try:
        sftp = client.open_sftp()
        try:
            try:
                top = sftp.stat(remote_path)
                if not stat.S_ISDIR(top.st_mode or 0):
                    raise SshError(
                        f"Remote target exists but is not a directory: "
                        f"{instance}:{remote_path}"
                    )
            except FileNotFoundError:
                _ensure_remote_dirs(sftp, remote_path)
                stats.dirs_created += 1
            _sync_dir_upload(sftp, instance, local_dir, remote_path, stats)
        finally:
            sftp.close()
        return stats
    finally:
        client.close()


def sync_from_remote(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    remote_path: str,
    local_dir: Path,
    *,
    deadline: float | None = None,
) -> SyncStats:
    """把远端目录镜像到本地（rclone sync 语义：删除本地多余条目）。

    ``remote_path`` 可以是绝对路径，也可以是相对于 SFTP 默认目录（登录用户
    家目录）的相对路径，与 ``cp`` / scp 一致。
    """
    _guard_remote_sync_target(remote_path)
    _guard_local_sync_target(local_dir)
    client = _connect(instance, host, user, key_path, deadline=deadline)
    stats = SyncStats()
    try:
        sftp = client.open_sftp()
        try:
            kind = _remote_kind(sftp, instance, remote_path)
            if not stat.S_ISDIR(kind):
                raise SshError(
                    f"Remote source is not a directory: {instance}:{remote_path}"
                )
            if local_dir.exists() and not local_dir.is_dir():
                raise SshError(
                    f"Local target exists but is not a directory: {local_dir}"
                )
            if not local_dir.exists():
                try:
                    local_dir.mkdir(parents=True)
                except OSError as exc:
                    raise SshError(
                        f"Cannot create local directory {local_dir}: "
                        f"{_local_reason(exc)}"
                    ) from exc
                stats.dirs_created += 1
            _sync_dir_download(sftp, instance, remote_path, local_dir, stats)
        finally:
            sftp.close()
        return stats
    finally:
        client.close()
