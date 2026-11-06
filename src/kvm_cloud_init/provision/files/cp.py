"""``cp`` 编排：经 SFTP 在宿主机与实例间拷贝文件或目录。

远程路径用 ``<实例名>:<路径>`` 标识，与 scp 习惯一致；源和目标中必须恰好
一个是远程路径（不支持两台实例之间对拷）。普通文件直接传输，目录递归
拷贝并保留符号链接。传输走 paramiko SFTP，不调用 ``scp`` / ``sftp`` /
``ssh`` 子进程。
"""

from __future__ import annotations

import posixpath
from pathlib import Path

import libvirt

from ... import ssh
from ...config import ConfigStore
from ...errors import ProvisionError
from . import paths


def copy_file(
    store: ConfigStore, conn: libvirt.virConnect, source: str, target: str
) -> int:
    """在本地与实例之间拷贝文件或目录，返回退出码。"""
    src = paths.parse_remote(source)
    dst = paths.parse_remote(target)
    if src is not None and dst is not None:
        raise ProvisionError(
            "Copying between two instances is not supported; exactly one side "
            "must be a local path"
        )
    if src is None and dst is None:
        raise ProvisionError(
            "At least one side must be an instance path in the form "
            "'<instance>:<path>'"
        )

    if src is None:
        return _upload(store, conn, Path(source), dst)  # type: ignore[arg-type]
    return _download(store, conn, src, Path(target))


def _kind_label(path: Path) -> str:
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "path"


def _upload(
    store: ConfigStore, conn: libvirt.virConnect, local: Path, remote: paths.RemotePath
) -> int:
    if not local.exists() and not local.is_symlink():
        raise ProvisionError(f"Local path not found: {local}")
    if not local.is_file() and not local.is_dir() and not local.is_symlink():
        raise ProvisionError(
            f"Unsupported local path type: {local} "
            "(only regular files, directories and symlinks are supported)"
        )
    host, user, key_path, deadline = paths._connect_target(
        store, conn, remote.instance
    )
    remote_target = remote.path
    # 与 scp -r 一致：目标是已存在的远端目录时，放入该目录并沿用本地名称
    if ssh.remote_is_directory(
        remote.instance, host, user, key_path, remote_target, deadline=deadline
    ):
        remote_target = posixpath.join(remote_target.rstrip("/"), local.name)
    count = ssh.upload_path(
        remote.instance,
        host,
        user,
        key_path,
        local,
        remote_target,
        deadline=deadline,
    )
    label = _kind_label(local)
    print(
        f"Uploaded {label} {local} -> {remote.instance}:{remote_target} "
        f"({count} file{'s' if count != 1 else ''})"
    )
    return 0


def _download(
    store: ConfigStore,
    conn: libvirt.virConnect,
    remote: paths.RemotePath,
    local: Path,
) -> int:
    host, user, key_path, deadline = paths._connect_target(
        store, conn, remote.instance
    )
    # 与 scp -r 一致：本地目标是已存在目录时，放入该目录并沿用远端名称
    local_target = (
        local / posixpath.basename(remote.path.rstrip("/"))
        if local.is_dir()
        else local
    )
    count = ssh.download_path(
        remote.instance,
        host,
        user,
        key_path,
        remote.path,
        local_target,
        deadline=deadline,
    )
    print(
        f"Downloaded {remote.instance}:{remote.path} -> {local_target} "
        f"({count} file{'s' if count != 1 else ''})"
    )
    return 0
