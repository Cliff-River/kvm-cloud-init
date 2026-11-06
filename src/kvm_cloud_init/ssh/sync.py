"""SFTP 镜像同步（sync 子命令；rclone sync 语义：目标成为源的镜像）。

能力层不负责方向解析与目标命名，那些属于 ``provision.files``。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

from ..errors import SshError
from . import client, sftp


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
    """同步会删除目标中的多余内容，本地根目录（含 ``..`` 变体）一律拒绝。"""
    if local_path.expanduser().resolve() == Path("/"):
        raise SshError("Refusing to sync to local path '/'")


def _resolved_remote_path(
    sftp_client: paramiko.SFTPClient, remote_path: str
) -> str | None:
    """返回 SFTP 规范化后的绝对路径；服务端不支持时返回 None。"""
    try:
        return sftp_client.normalize(remote_path)
    except (OSError, AttributeError):
        return None


def _guard_remote_sync_target_resolved(
    sftp_client: paramiko.SFTPClient, instance: str, remote_path: str
) -> None:
    """用规范化结果拦截 ``..`` 绕过的根目录/家目录目标。"""
    target = _resolved_remote_path(sftp_client, remote_path)
    if target is None:
        return
    if target == "/":
        raise SshError("Refusing to sync to remote path '/'")
    home = _resolved_remote_path(sftp_client, ".")
    if home is None:
        return
    if target == home or home.startswith(target.rstrip("/") + "/"):
        raise SshError(
            f"Refusing to sync to the remote home directory ({instance}:{home})"
        )


def _remote_list(
    sftp_client: paramiko.SFTPClient, instance: str, remote_dir: str
) -> dict[str, paramiko.SFTPAttributes]:
    """lstat 列出远端目录条目（不跟随符号链接）；目录不存在抛 SshError。"""
    try:
        entries = sftp_client.listdir_attr(remote_dir)
    except FileNotFoundError as exc:
        raise SshError(
            f"Remote path not found: {instance}:{remote_dir}"
        ) from exc
    except OSError as exc:
        raise SshError(
            f"Cannot list remote directory {instance}:{remote_dir}: "
            f"{sftp._sftp_reason(exc)}"
        ) from exc
    return {entry.filename: entry for entry in entries}


def _remove_remote_tree(
    sftp_client: paramiko.SFTPClient,
    instance: str,
    remote_path: str,
    mode: int,
    stats: SyncStats,
) -> None:
    """递归删除远端目录树或删除单个文件/符号链接。"""
    try:
        if stat.S_ISDIR(mode):
            for entry in sftp_client.listdir_attr(remote_path):
                child = f"{remote_path.rstrip('/')}/{entry.filename}"
                _remove_remote_tree(
                    sftp_client,
                    instance,
                    child,
                    stat.S_IFMT(entry.st_mode or 0),
                    stats,
                )
            sftp_client.rmdir(remote_path)
        else:
            sftp_client.remove(remote_path)
        stats.deleted_entries += 1
        stats.deleted.append(f"{instance}:{remote_path}")
    except OSError as exc:
        raise SshError(
            f"Cannot delete {instance}:{remote_path}: "
            f"{sftp._sftp_reason(exc)}"
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
        raise SshError(
            f"Cannot delete local path {path}: {sftp._local_reason(exc)}"
        ) from exc


def _sync_dir_upload(
    sftp_client: paramiko.SFTPClient,
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
            f"Cannot list local directory {src_dir}: {sftp._local_reason(exc)}"
        ) from exc

    dest_entries = _remote_list(sftp_client, instance, dst_dir)

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
                    and _safe_readlink(sftp_client, dst_path) == link_target
                )
                if not same_link:
                    if dst_attrs is not None:
                        _remove_remote_tree(
                            sftp_client, instance, dst_path, dst_kind, stats
                        )  # type: ignore[arg-type]
                    sftp_client.symlink(link_target, dst_path)
                    stats.symlinks_created += 1
            elif src_entry.is_dir(follow_symlinks=False):
                if dst_kind is not None and not stat.S_ISDIR(dst_kind):
                    _remove_remote_tree(
                        sftp_client, instance, dst_path, dst_kind, stats
                    )  # type: ignore[arg-type]
                    dst_kind = None
                if dst_kind is None:
                    sftp._ensure_remote_dirs(sftp_client, dst_path)
                    stats.dirs_created += 1
                _sync_dir_upload(
                    sftp_client, instance, src_path, dst_path, stats
                )
            elif src_entry.is_file(follow_symlinks=False):
                src_stat = src_entry.stat(follow_symlinks=False)
                outdated = (
                    dst_kind is None
                    or not stat.S_ISREG(dst_kind)
                    or dst_attrs.st_size != src_stat.st_size
                    or int(dst_attrs.st_mtime or 0) != int(src_stat.st_mtime)
                )
                if dst_kind is not None and not stat.S_ISREG(dst_kind):
                    _remove_remote_tree(
                        sftp_client, instance, dst_path, dst_kind, stats
                    )  # type: ignore[arg-type]
                    outdated = True
                if outdated:
                    sftp_client.put(str(src_path), dst_path)
                    # 保留源 mtime，使下一次 sync 可凭 size+mtime 跳过
                    sftp_client.utime(
                        dst_path, (src_stat.st_mtime, src_stat.st_mtime)
                    )
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
                f"{sftp._sftp_reason(exc)}"
            ) from exc

    # 目标中剩余条目在源中已不存在：递归删除
    for name, dst_attrs in dest_entries.items():
        _remove_remote_tree(
            sftp_client,
            instance,
            f"{dst_dir.rstrip('/')}/{name}",
            stat.S_IFMT(dst_attrs.st_mode or 0),
            stats,
        )


def _safe_readlink(
    sftp_client: paramiko.SFTPClient, remote_path: str
) -> str | None:
    try:
        return sftp_client.readlink(remote_path)
    except OSError:
        return None


def _sync_dir_download(
    sftp_client: paramiko.SFTPClient,
    instance: str,
    src_dir: str,
    dst_dir: Path,
    stats: SyncStats,
) -> None:
    """把远端 ``src_dir`` 的内容镜像到本地 ``dst_dir``（双向类型替换）。"""
    source_entries = _remote_list(sftp_client, instance, src_dir)

    try:
        dest_names = set(os.listdir(dst_dir))
    except FileNotFoundError as exc:
        raise SshError(f"Local path not found: {dst_dir}") from exc
    except OSError as exc:
        raise SshError(
            f"Cannot list local directory {dst_dir}: {sftp._local_reason(exc)}"
        ) from exc

    for name, src_attrs in sorted(source_entries.items()):
        src_path = f"{src_dir.rstrip('/')}/{name}"
        dst_path = dst_dir / name
        dest_names.discard(name)
        src_kind = stat.S_IFMT(src_attrs.st_mode or 0)
        try:
            if stat.S_ISLNK(src_kind):
                link_target = sftp_client.readlink(src_path)
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
                _sync_dir_download(
                    sftp_client, instance, src_path, dst_path, stats
                )
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
                    sftp_client.get(src_path, str(dst_path))
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
                f"{sftp._sftp_reason(exc)}"
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
    ssh_client = client._connect(
        instance, host, user, key_path, deadline=deadline
    )
    stats = SyncStats()
    try:
        sftp_client = ssh_client.open_sftp()
        try:
            _guard_remote_sync_target_resolved(
                sftp_client, instance, remote_path
            )
            try:
                top = sftp_client.stat(remote_path)
                if not stat.S_ISDIR(top.st_mode or 0):
                    raise SshError(
                        f"Remote target exists but is not a directory: "
                        f"{instance}:{remote_path}"
                    )
            except FileNotFoundError:
                sftp._ensure_remote_dirs(sftp_client, remote_path)
                stats.dirs_created += 1
            _sync_dir_upload(
                sftp_client, instance, local_dir, remote_path, stats
            )
        finally:
            sftp_client.close()
        return stats
    finally:
        ssh_client.close()


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
    ssh_client = client._connect(
        instance, host, user, key_path, deadline=deadline
    )
    stats = SyncStats()
    try:
        sftp_client = ssh_client.open_sftp()
        try:
            if _resolved_remote_path(sftp_client, remote_path) == "/":
                raise SshError("Refusing to sync from remote path '/'")
            kind = sftp._remote_kind(sftp_client, instance, remote_path)
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
                        f"{sftp._local_reason(exc)}"
                    ) from exc
                stats.dirs_created += 1
            _sync_dir_download(
                sftp_client, instance, remote_path, local_dir, stats
            )
        finally:
            sftp_client.close()
        return stats
    finally:
        ssh_client.close()
