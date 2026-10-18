"""实测 IP 获取（source 回退链）测试，全部 mock libvirt。"""

from __future__ import annotations

import libvirt
import pytest

from kvm_cloud_init.errors import NetworkError
from kvm_cloud_init.hv import network

LEASE = libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE
AGENT = libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
ARP = libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP


def _iface(ip: str, addr_type: int = libvirt.VIR_IP_ADDR_TYPE_IPV4) -> dict:
    return {"eth0": {"name": "eth0", "hwaddr": "52:54:00:00:00:01",
                     "addrs": [{"type": addr_type, "addr": ip, "prefix": 24}]}}


class _Dom:
    def __init__(self, state: int, results: dict[int, object], addrs=None) -> None:
        self._state = state
        self._results = results
        self._addrs = addrs
        self.calls: list[int] = []

    def state(self, flags: int) -> list[int]:
        return [self._state]

    def interfaceAddresses(self, source: int, flags: int):
        self.calls.append(source)
        if self._addrs is not None:
            return self._addrs
        result = self._results.get(source)
        if result is None:
            raise libvirt.libvirtError("no address")
        return result


class _Conn:
    def __init__(self, dom: _Dom | None) -> None:
        self._dom = dom

    def lookupByName(self, name: str) -> _Dom:
        if self._dom is None:
            raise libvirt.libvirtError("domain not found")
        return self._dom


RUNNING = libvirt.VIR_DOMAIN_RUNNING


def test_prefers_lease_source() -> None:
    dom = _Dom(RUNNING, {LEASE: _iface("192.168.122.11")})
    assert network.find_ipv4(_Conn(dom), "vm1") == "192.168.122.11"
    assert dom.calls == [LEASE]


def test_falls_back_to_agent_then_arp() -> None:
    dom = _Dom(RUNNING, {ARP: _iface("192.168.122.13")})
    assert network.find_ipv4(_Conn(dom), "vm1") == "192.168.122.13"
    assert dom.calls == [LEASE, AGENT, ARP]


def test_skips_loopback_and_ipv6() -> None:
    addrs = {
        "lo": {"name": "lo", "addrs": [
            {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "127.0.0.1", "prefix": 8},
        ]},
        "eth0": {"name": "eth0", "addrs": [
            {"type": libvirt.VIR_IP_ADDR_TYPE_IPV6, "addr": "fe80::1", "prefix": 64},
            {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "10.0.0.9", "prefix": 24},
        ]},
    }
    dom = _Dom(RUNNING, {}, addrs=addrs)
    assert network.find_ipv4(_Conn(dom), "vm1") == "10.0.0.9"


def test_timeout_raises_network_error() -> None:
    dom = _Dom(RUNNING, {})
    with pytest.raises(NetworkError, match="60|未获得"):
        # timeout=0 时先查询一次即超时
        network.find_ipv4(_Conn(dom), "vm1", timeout=0,
                          clock=lambda: 0.0, sleep=lambda s: None)


def test_missing_domain_raises() -> None:
    with pytest.raises(NetworkError, match="不存在"):
        network.find_ipv4(_Conn(None), "ghost")


def test_shutoff_domain_raises() -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_SHUTOFF, {})
    with pytest.raises(NetworkError, match="运行状态"):
        network.find_ipv4(_Conn(dom), "vm1")
