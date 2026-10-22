"""实测 IP 获取：只信任 libvirt 域的接口地址，不信任 network-config 声明。

使用 libvirt 的 ``virDomainInterfaceAddresses``（等价 ``virsh domifaddr``），
按 source 顺序回退：DHCP 租约 -> qemu-guest-agent -> ARP 主动探测。
虚拟机启动后 IP 不会立即就绪，因此带超时轮询。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import libvirt

from ..errors import NetworkError

#: source 回退顺序：lease -> agent -> arp
SOURCES: tuple[int, ...] = (
    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP,
)

#: 轮询缺省参数（R5：60 秒、每 2 秒一次）
DEFAULT_TIMEOUT = 60
DEFAULT_INTERVAL = 2


def lookup_domain(conn: libvirt.virConnect, name: str) -> libvirt.virDomain | None:
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError:
        return None


def _pick_ipv4(interfaces: dict) -> str | None:
    """从接口地址表中取首个非回环 IPv4。"""
    for iface in interfaces.values():
        for addr in iface.get("addrs") or []:
            if addr.get("type") != libvirt.VIR_IP_ADDR_TYPE_IPV4:
                continue
            value = addr.get("addr")
            if not value or value.startswith("127."):
                continue
            return value
    return None


def _query(dom: libvirt.virDomain) -> str | None:
    """按 source 顺序尝试各类接口地址查询。"""
    for source in SOURCES:
        try:
            interfaces = dom.interfaceAddresses(source, 0)
        except libvirt.libvirtError:
            continue
        found = _pick_ipv4(interfaces)
        if found:
            return found
    return None


def find_ipv4(
    conn: libvirt.virConnect,
    name: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    interval: int = DEFAULT_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """返回域的首个可用 IPv4 地址，超时抛 NetworkError。"""
    dom = lookup_domain(conn, name)
    if dom is None:
        raise NetworkError(f"实例 {name} 不存在，无法查询 IP 地址")
    if dom.state(0)[0] != libvirt.VIR_DOMAIN_RUNNING:
        raise NetworkError(f"实例 {name} 未处于运行状态，无法查询 IP 地址")

    deadline = clock() + timeout
    while True:
        found = _query(dom)
        if found:
            return found
        if clock() >= deadline:
            raise NetworkError(
                f"实例 {name} 在 {timeout} 秒内未获得可用 IP 地址；"
                "请确认网络（DHCP）、已安装 qemu-guest-agent，"
                "或用 virsh domifaddr "
                f"{name} --source lease|agent|arp 进一步排查"
            )
        sleep(interval)
