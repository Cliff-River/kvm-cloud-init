"""``mount`` / ``unmount`` 编排：9p 设备热插拔 + guest 内挂载。

宿主机侧（libvirt 9p filesystem 设备）由 ``hv.filesystem`` 负责；本模块
通过既有 paramiko 通道在 guest 内执行 ``mkdir`` / ``mount`` / ``umount``。
SSH 用户为 root 时直接执行，否则仅使用 ``sudo -n``（免密 sudo），
绝不卡在交互式密码提示上，也不调用任何子进程。
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

import libvirt

from .. import ssh
from ..config import ConfigStore
from ..errors import MountError
from ..hv import domains, filesystem
from . import access

#: 等待实例 SSH 可达的窗口（与 shell/exec 一致）
_SSH_READY_TIMEOUT = access.SSH_READY_TIMEOUT

#: guest 内 9p 挂载选项：virtio-9p PCI 传输 + Linux 扩展协议 + 较大 msize
_MOUNT_OPTIONS = "trans=virtio,version=9p2000.L,msize=262144"


def _validate_guest_path(value: str) -> str:
    if not value.startswith("/") or value == "/":
        raise MountError(f"Guest mount path must be an absolute path other than '/': {value}")
    return value.rstrip("/")


def _shell_command(argv: list[str], root: bool) -> str:
    command = " ".join(shlex.quote(part) for part in argv)
    return command if root else f"sudo -n {command}"


def _domain_is_running(dom: libvirt.virDomain) -> bool:
    return dom.state(0)[0] == libvirt.VIR_DOMAIN_RUNNING


def _run_remote(
    name: str,
    host: str,
    user: str,
    key_path: Path,
    argv: list[str],
    *,
    root: bool,
    deadline: float,
) -> tuple[str, str, int]:
    return ssh.capture_command(
        name,
        host,
        user,
        key_path,
        _shell_command(argv, root),
        deadline=deadline,
    )


def _is_root_user(
    name: str, host: str, user: str, key_path: Path, *, deadline: float
) -> bool:
    out, _err, rc = ssh.capture_command(
        name, host, user, key_path, "id -u", deadline=deadline
    )
    if rc != 0:
        raise MountError(
            f"Cannot determine the remote user id in instance {name} "
            f"(exit {rc})"
        )
    return out.strip() == "0"


def mount_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    local_dir: str,
    guest_dir: str,
) -> int:
    """把宿主机目录通过 9p 挂载到运行实例内的绝对路径。"""
    source = Path(local_dir)
    if not source.is_dir():
        raise MountError(f"Local directory does not exist: {source}")
    target = _validate_guest_path(guest_dir)

    dom = domains.lookup_domain(conn, name)
    if dom is None:
        raise MountError(f"Instance {name} does not exist")
    # 持久配置中已登记同一路径时直接拒绝，避免依赖运行态 XML 做去重
    if filesystem.find_mount(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE), target):
        raise MountError(f"{target} is already mounted in instance {name}")

    # 复用 shell/exec 的自动启动与 IP/sshd 等待逻辑，不另起一套生命周期
    access._ensure_running(store, conn, name)
    dom = domains.lookup_domain(conn, name)

    source_path = str(source.resolve())
    info = filesystem.add_mount(
        conn, dom, source_path, target, running=_domain_is_running(dom)
    )

    deadline = time.monotonic() + _SSH_READY_TIMEOUT
    success = False
    try:
        host, user, key_path = access.wait_ssh_target(conn, name, deadline=deadline)
        root = _is_root_user(name, host, user, key_path, deadline=deadline)
        _out, err, rc = _run_remote(
            name, host, user, key_path,
            ["mkdir", "-p", target], root=root, deadline=deadline,
        )
        if rc != 0:
            raise MountError(
                f"Cannot create mount point {target} in instance {name}: "
                f"{err.strip() or f'exit {rc}'}"
            )
        _out, err, rc = _run_remote(
            name, host, user, key_path,
            ["mount", "-t", "9p", "-o", _MOUNT_OPTIONS, info.tag, target],
            root=root,
            deadline=deadline,
        )
        if rc != 0:
            raise MountError(
                f"Cannot mount {source_path} at {target} in instance {name}: "
                f"{err.strip() or f'exit {rc}'}. "
                "Ensure the guest kernel supports 9p (CONFIG_9P_FS / "
                "CONFIG_NET_9P_VIRTIO) and the SSH user has passwordless sudo."
            )
        success = True
    finally:
        if not success:
            # guest 内挂载失败：回滚已热插的设备与持久配置，避免悬挂状态
            try:
                filesystem.remove_mount(
                    conn, dom, target, running=_domain_is_running(dom)
                )
            except Exception as exc:  # noqa: BLE001 - 回滚失败只告警
                print(
                    f"Warning: failed to roll back the 9p device for {target}: "
                    f"{exc}",
                    flush=True,
                )

    print(f"Mounted {source_path} -> {name}:{target} (tag {info.tag}, 9p)")
    return 0


def unmount_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    guest_dir: str,
) -> int:
    """卸载实例内的共享挂载并从域配置移除 9p 设备。"""
    target = _validate_guest_path(guest_dir)
    dom = domains.lookup_domain(conn, name)
    if dom is None:
        raise MountError(f"Instance {name} does not exist")

    info = filesystem.find_mount(
        dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE), target
    )
    if info is None:
        raise MountError(f"No mounted host directory found at {target} in {name}")

    running = _domain_is_running(dom)
    if running:
        # 实例运行时先在 guest 内 umount；busy 等失败必须中止，
        # 不能直接 detach 设备（可能造成 guest IO 错误）
        deadline = time.monotonic() + _SSH_READY_TIMEOUT
        host, user, key_path = access.ssh_target(conn, name)
        root = _is_root_user(name, host, user, key_path, deadline=deadline)
        _out, err, rc = _run_remote(
            name, host, user, key_path,
            ["umount", target], root=root, deadline=deadline,
        )
        if rc != 0:
            raise MountError(
                f"Cannot unmount {target} in instance {name}: "
                f"{err.strip() or f'exit {rc}'}"
            )

    filesystem.remove_mount(conn, dom, target, running=running)
    print(f"Unmounted {info.source} from {name}:{target}")
    return 0
