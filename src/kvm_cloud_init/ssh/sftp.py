"""SFTP 文件传输（cp 子命令；不调用 scp/sftp/ssh 子进程）。

支持普通文件、目录递归与符号链接；远端父目录自动逐级创建。
"""

from __future__ import annotations

import os
import posixpath
import stat
from pathlib import Path

import paramiko

from ..errors import SshError
from . import client


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
    ssh_client = client._connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = ssh_client.open_sftp()
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
        ssh_client.close()


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
    ssh_client = client._connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = ssh_client.open_sftp()
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
        ssh_client.close()


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
    ssh_client = client._connect(instance, host, user, key_path, deadline=deadline)
    try:
        sftp = ssh_client.open_sftp()
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
        ssh_client.close()
