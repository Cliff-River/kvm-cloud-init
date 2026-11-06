"""``sync`` 编排：把目录镜像同步到对端（``rclone sync`` 语义）。

与 ``cp`` 不同：
- 源与目标都必须是目录，同步的是源目录的 **内容**；
- 目标中源里已不存在的文件、目录与符号链接会被删除；
- 未变化的普通文件（size + mtime 相同）不重复传输。

传输仍走 paramiko SFTP，不调用 ``rsync`` / ``rclone`` / ``ssh`` 子进程。
"""

from __future__ import annotations

import posixpath
from pathlib import Path

import libvirt

from ... import ssh
from ...config import ConfigStore
from ...errors import ProvisionError
from . import paths

#: 没有有意义目录名的路径（根、当前目录、家目录）
_NAMELESS_PATHS = frozenset({"", ".", "..", "~"})


def _source_basename(path: str) -> str:
    """返回远端源目录名；先规范化再取最后一段（``site/.`` 也算 ``site``）。"""
    name = posixpath.basename(posixpath.normpath(path))
    return "" if name in _NAMELESS_PATHS else name


def _local_source_basename(path: str) -> str:
    """返回本地源目录名；``.``/``..``/``~`` 先解析成真实目录再取最后一段。"""
    name = Path(path).expanduser().resolve().name
    return "" if name in _NAMELESS_PATHS else name


def _local_target(target: Path, source_name: str) -> Path:
    """目标目录名与源目录名不同时，改为目标下的同名子目录。

    这是防止 ``sync <源目录> ~/`` 把整个家目录镜像清空的保护：
    目标是 ``~``（名称与源不同）时，实际同步到 ``~/<源目录名>``。
    先 ``expanduser``，避免被引号包住的 ``~`` 被当成字面目录名。
    """
    target = target.expanduser()
    if not source_name or target.name == source_name:
        return target
    return target / source_name


def _remote_target(target: str, source_name: str) -> str:
    """远端目标同理：名称不同则落到目标下的同名子目录（保留根/相对形态）。"""
    if not source_name:
        return target
    if posixpath.basename(posixpath.normpath(target)) == source_name:
        return target
    return posixpath.join(target, source_name)


def _reject_tilde_path(remote: paths.RemotePath | None, side: str) -> None:
    """SFTP 不会展开 ``~``；带 ``~`` 的远端路径会被当成字面目录，直接拒绝。"""
    if remote is not None and remote.path.startswith("~"):
        raise ProvisionError(
            f"Remote {side} paths starting with '~' are not supported: "
            f"{remote.instance}:{remote.path}; use an absolute path or a path "
            "relative to the home directory"
        )


def _has_empty_remote_path(spec: str) -> bool:
    """``<实例>:`` 这种空远端路径会被 parse_remote 当成本地路径，需单独识别。"""
    return ":" in spec and spec.split(":", 1)[1] == ""


def sync_paths(
    store: ConfigStore, conn: libvirt.virConnect, source: str, target: str
) -> int:
    """把源目录镜像到目标目录，返回退出码。"""
    if _has_empty_remote_path(source) or _has_empty_remote_path(target):
        raise ProvisionError(
            "Remote path is empty; use '<instance>:<path>' (use a relative "
            "path such as '<instance>:.' for the home directory)"
        )
    src = paths.parse_remote(source)
    dst = paths.parse_remote(target)
    _reject_tilde_path(src, "source")
    _reject_tilde_path(dst, "target")
    if src is not None and dst is not None:
        raise ProvisionError(
            "Syncing between two instances is not supported; exactly one side "
            "must be a local path"
        )
    if src is None and dst is None:
        raise ProvisionError(
            "At least one side must be an instance path in the form "
            "'<instance>:<path>'"
        )

    if src is None:
        if dst.path == "/":
            raise ProvisionError("Refusing to sync to remote path '/'")
        remote = paths.RemotePath(
            dst.instance,
            _remote_target(dst.path, _local_source_basename(source)),
        )
        return _sync_up(store, conn, Path(source), remote)

    local_target = Path(target)
    if local_target.expanduser().resolve() == Path("/"):
        raise ProvisionError("Refusing to sync to local path '/'")
    local_target = _local_target(local_target, _source_basename(src.path))
    return _sync_down(store, conn, src, local_target)


def _summary(stats: ssh.SyncStats) -> str:
    return (
        f"{stats.files_transferred} file"
        f"{'s' if stats.files_transferred != 1 else ''} transferred, "
        f"{stats.dirs_created} director"
        f"{'ies' if stats.dirs_created != 1 else 'y'} created, "
        f"{stats.symlinks_created} symlink"
        f"{'s' if stats.symlinks_created != 1 else ''}, "
        f"{stats.deleted_entries} entr"
        f"{'ies' if stats.deleted_entries != 1 else 'y'} deleted"
    )


def _sync_up(
    store: ConfigStore,
    conn: libvirt.virConnect,
    local: Path,
    remote: paths.RemotePath,
) -> int:
    if not local.is_dir():
        raise ProvisionError(f"Local source directory not found: {local}")
    host, user, key_path, deadline = paths._connect_target(
        store, conn, remote.instance
    )
    stats = ssh.sync_to_remote(
        remote.instance,
        host,
        user,
        key_path,
        local,
        remote.path,
        deadline=deadline,
    )
    print(
        f"Synced {local} -> {remote.instance}:{remote.path} ({_summary(stats)})"
    )
    for deleted in stats.deleted:
        print(f"  deleted {deleted}")
    return 0


def _sync_down(
    store: ConfigStore,
    conn: libvirt.virConnect,
    remote: paths.RemotePath,
    local: Path,
) -> int:
    host, user, key_path, deadline = paths._connect_target(
        store, conn, remote.instance
    )
    stats = ssh.sync_from_remote(
        remote.instance,
        host,
        user,
        key_path,
        remote.path,
        local,
        deadline=deadline,
    )
    print(
        f"Synced {remote.instance}:{remote.path} -> {local} ({_summary(stats)})"
    )
    for deleted in stats.deleted:
        print(f"  deleted {deleted}")
    return 0
