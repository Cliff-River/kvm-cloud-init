"""``<实例名>:<路径>`` 解析与远端目标连接准备。

``cp`` 与 ``sync`` 共用：校验实例存在、必要时自动启动、等待 SSH 就绪。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import libvirt

from ...config import ConfigStore
from ...errors import ProvisionError
from ...hv import domains
from .. import access

#: 等待实例 SSH 可达的窗口（与 shell/exec 一致）
_SSH_READY_TIMEOUT = access.SSH_READY_TIMEOUT


@dataclass(frozen=True)
class RemotePath:
    """``<实例名>:<guest 内路径>`` 解析结果。"""

    instance: str
    path: str


def parse_remote(spec: str) -> RemotePath | None:
    """把 ``vm1:/etc/hosts`` 解析为 RemotePath；本地路径返回 None。"""
    if ":" not in spec:
        return None
    instance, path = spec.split(":", 1)
    if not instance or not path:
        return None
    return RemotePath(instance=instance, path=path)


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
