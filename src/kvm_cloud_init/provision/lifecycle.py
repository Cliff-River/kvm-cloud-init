"""实例生命周期：start / stop / reboot / status / destroy。"""

from __future__ import annotations

import libvirt

from .. import ssh
from ..config import ConfigStore
from ..errors import NetworkError, ProvisionError
from ..hv import domains, network
from ..hv.client import DOMAIN_STATE_NAMES
from .guard import ensure_destroy_allowed


def _require_domain(conn: libvirt.virConnect, name: str) -> libvirt.virDomain:
    dom = domains.lookup_domain(conn, name)
    if dom is None:
        raise ProvisionError(f"Instance {name} does not exist")
    return dom


def _shutdown_timeout(store: ConfigStore, name: str) -> int:
    if name in store.instances:
        return store.resolve(name).shutdown_timeout
    return store.defaults.shutdown_timeout


def start_instance(store: ConfigStore, conn: libvirt.virConnect, name: str) -> None:
    """启动已定义的实例；已运行时幂等返回。"""
    dom = _require_domain(conn, name)
    if dom.state(0)[0] == libvirt.VIR_DOMAIN_RUNNING:
        print(f"Instance {name} is already running.")
        return
    try:
        dom.create(0)
    except libvirt.libvirtError as exc:
        raise ProvisionError(f"Cannot start instance {name}: {exc}") from exc
    print(f"Instance {name} started.")


def stop_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    force: bool = False,
) -> None:
    """优雅关闭实例；``--force`` 仅解除超时后的强制断电限制。"""
    dom = _require_domain(conn, name)
    if dom.state(0)[0] == libvirt.VIR_DOMAIN_SHUTOFF:
        print(f"Instance {name} is already shut off.")
        return
    timeout = _shutdown_timeout(store, name)
    domains.stop_domain(conn, name, timeout, force)
    print(f"Instance {name} stopped.")


def reboot_instance(store: ConfigStore, conn: libvirt.virConnect, name: str) -> None:
    """对运行中的实例发送 ACPI reboot。"""
    dom = _require_domain(conn, name)
    if dom.state(0)[0] != libvirt.VIR_DOMAIN_RUNNING:
        raise ProvisionError(
            f"Instance {name} is not running; "
            f"use 'kvmci start {name}' first"
        )
    try:
        dom.reboot(0)
    except libvirt.libvirtError as exc:
        raise ProvisionError(f"Cannot reboot instance {name}: {exc}") from exc
    print(f"Instance {name} is rebooting.")


def status_instance(
    store: ConfigStore, conn: libvirt.virConnect, name: str
) -> None:
    """打印单个实例的实时状态（只读）。"""
    dom = _require_domain(conn, name)
    info = dom.info()
    state = DOMAIN_STATE_NAMES.get(info[0], "unknown")
    template = store.instances[name].template if name in store.instances else "-"

    ipv4 = "-"
    if info[0] == libvirt.VIR_DOMAIN_RUNNING:
        try:
            ipv4 = network.find_ipv4(conn, name)
        except NetworkError:
            ipv4 = "-"

    print(f"Name:     {name}")
    print(f"Template: {template}")
    print(f"State:    {state}")
    print(f"vCPUs:    {info[3]}")
    print(f"Memory:   {info[1] // 1024} MiB")
    print(f"IPv4:     {ipv4}")


def destroy_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> bool:
    """销毁实例。返回是否找到了同名域。

    ``yes`` 跳过 production 级别的交互确认；``force`` 仍是优雅关机超时后
    强制断电的既有语义；protected 级别一律拒绝。未登记实例按 normal 处理。
    """
    resolved = None
    if name in store.instances:
        resolved = store.resolve(name)
    level = resolved.level if resolved else "normal"
    ensure_destroy_allowed(
        name, level, skip_confirm=yes, action="destroy the instance"
    )
    timeout = resolved.shutdown_timeout if resolved else store.defaults.shutdown_timeout
    pool_name = resolved.storage_pool if resolved else store.defaults.storage_pool

    existed = domains.destroy_domain(conn, name, pool_name, timeout, force)
    if not existed:
        print(f"Instance {name} does not exist; nothing to destroy.")
    else:
        print(f"Instance {name} destroyed.")

    _cleanup_keypair(name)
    return existed


def _cleanup_keypair(name: str) -> None:
    """销毁后清理程序生成的密钥；删除失败不阻断流程，仅输出警告。"""
    try:
        ssh.delete_keypair(name)
    except OSError as exc:
        print(
            f"Warning: failed to delete SSH keys for instance {name}: {exc}; "
            "please remove them manually."
        )
