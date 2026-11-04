"""``sync`` 编排：把目录镜像同步到对端（``rclone sync`` 语义）。

与 ``cp`` 不同：
- 源与目标都必须是目录，同步的是源目录的 **内容**；
- 目标中源里已不存在的文件、目录与符号链接会被删除；
- 未变化的普通文件（size + mtime 相同）不重复传输。

传输仍走 paramiko SFTP，不调用 ``rsync`` / ``rclone`` / ``ssh`` 子进程。
"""

from __future__ import annotations

import time
from pathlib import Path

import libvirt

from .. import ssh
from ..config import ConfigStore
from ..errors import ProvisionError
from ..hv import domains
from . import access
from .transfer import RemotePath, parse_remote

#: 等待实例 SSH 可达的窗口（与 shell/exec/cp 一致）
_SSH_READY_TIMEOUT = access.SSH_READY_TIMEOUT


def _connect_target(
    store: ConfigStore, conn: libvirt.virConnect, instance: str
) -> tuple[str, str, Path, float]:
    """校验实例存在、必要时自动启动，并等待 SSH 就绪。"""
    dom = domains.lookup_domain(conn, instance)
    if dom is None:
        raise ProvisionError(f"Instance {instance} does not exist")
    access._ensure_running(store, conn, instance)
    deadline = time.monotonic() + _SSH_READY_TIMEOUT
    host, user, key_path = access.wait_ssh_target(
        conn, instance, deadline=deadline
    )
    return host, user, key_path, deadline


def sync_paths(
    store: ConfigStore, conn: libvirt.virConnect, source: str, target: str
) -> int:
    """把源目录镜像到目标目录，返回退出码。"""
    src = parse_remote(source)
    dst = parse_remote(target)
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
        return _sync_up(store, conn, Path(source), dst)  # type: ignore[arg-type]
    return _sync_down(store, conn, src, Path(target))


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
    remote: RemotePath,
) -> int:
    if not local.is_dir():
        raise ProvisionError(f"Local source directory not found: {local}")
    host, user, key_path, deadline = _connect_target(store, conn, remote.instance)
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
    remote: RemotePath,
    local: Path,
) -> int:
    host, user, key_path, deadline = _connect_target(store, conn, remote.instance)
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
