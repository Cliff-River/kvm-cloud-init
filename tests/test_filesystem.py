"""9p filesystem 设备的 XML 登记 / 热插拔测试（fake conn/dom，不连 libvirt）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from unittest.mock import patch

import pytest

from kvm_cloud_init.errors import MountError
from kvm_cloud_init.hv import filesystem as fsmod
from kvm_cloud_init.hv.guest import METADATA_NS, build_domain_xml


def _base_xml() -> str:
    with patch(
        "kvm_cloud_init.hv.guest.resolve_os_id",
        return_value="http://rockylinux.org/rocky/10",
    ):
        return build_domain_xml(
            name="vm1",
            disk_path="/pool/vm1.qcow2",
            iso_path="/pool/vm1-cidata.iso",
            memory_mib=1024,
            vcpus=1,
            network="default",
            firmware="bios",
            os_variant="rocky10",
            template_name="Rocky-LVM",
            ssh_user="cliff",
        )


class FakeDom:
    def __init__(self, xml: str, name: str = "vm1") -> None:
        self._xml = xml
        self._name = name
        self.attached: list[str] = []
        self.detached: list[str] = []

    def name(self) -> str:
        return self._name

    def XMLDesc(self, _flags: int) -> str:
        return self._xml

    def attachDeviceFlags(self, xml: str, _flags: int) -> None:
        self.attached.append(xml)

    def detachDeviceFlags(self, xml: str, _flags: int) -> None:
        self.detached.append(xml)


class FakeConn:
    def __init__(self, dom: FakeDom) -> None:
        self.dom = dom
        self.defined: list[str] = []

    def defineXML(self, xml: str) -> FakeDom:
        self.defined.append(xml)
        self.dom._xml = xml
        return self.dom


@pytest.fixture
def env() -> tuple[FakeConn, FakeDom]:
    dom = FakeDom(_base_xml())
    return FakeConn(dom), dom


def test_build_filesystem_xml() -> None:
    fragment = ET.fromstring(fsmod.build_filesystem_xml("/host/share", "kci_abcd"))
    assert fragment.get("type") == "mount"
    assert fragment.get("accessmode") == "passthrough"
    assert fragment.find("driver").get("type") == "path"
    assert fragment.find("source").get("dir") == "/host/share"
    assert fragment.find("target").get("dir") == "kci_abcd"


def test_list_mounts_empty_on_fresh_domain(env) -> None:
    _conn, dom = env
    assert fsmod.list_mounts(dom.XMLDesc(0)) == []


def test_add_mount_persists_and_attaches_when_running(env) -> None:
    conn, dom = env
    tag = fsmod.mount_tag("vm1", "/mnt/share")

    info = fsmod.add_mount(
        conn, dom, "/host/share", "/mnt/share", running=True
    )

    assert info == fsmod.MountInfo(tag=tag, source="/host/share", target="/mnt/share")
    assert len(conn.defined) == 1
    assert len(dom.attached) == 1
    assert tag in dom.attached[0]

    root = ET.fromstring(conn.defined[-1])
    filesystem = root.find("./devices/filesystem")
    assert filesystem is not None
    assert filesystem.find("source").get("dir") == "/host/share"
    assert filesystem.find("target").get("dir") == tag
    mount = root.find(f".//{{{METADATA_NS}}}mount")
    assert mount is not None
    assert mount.get("tag") == tag
    assert mount.get("source") == "/host/share"
    assert mount.get("target") == "/mnt/share"


def test_add_mount_shut_off_does_not_attach_live(env) -> None:
    conn, dom = env
    fsmod.add_mount(conn, dom, "/host/share", "/mnt/share", running=False)
    assert dom.attached == []
    assert len(conn.defined) == 1


def test_add_duplicate_mount_rejected(env) -> None:
    conn, dom = env
    fsmod.add_mount(conn, dom, "/host/a", "/mnt/share", running=False)
    with pytest.raises(MountError, match="already mounted"):
        fsmod.add_mount(conn, dom, "/host/b", "/mnt/share", running=False)
    # 重复挂载不得产生新的 defineXML
    assert len(conn.defined) == 1


def test_remove_mount_persists_and_detaches(env) -> None:
    conn, dom = env
    info = fsmod.add_mount(
        conn, dom, "/host/share", "/mnt/share", running=True
    )

    removed = fsmod.remove_mount(conn, dom, "/mnt/share", running=True)

    assert removed == info
    assert len(dom.detached) == 1
    assert info.tag in dom.detached[0]
    root = ET.fromstring(conn.defined[-1])
    assert root.find("./devices/filesystem") is None
    assert fsmod.list_mounts(conn.defined[-1]) == []


def test_remove_missing_mount_raises(env) -> None:
    conn, dom = env
    with pytest.raises(MountError, match="No mounted host directory"):
        fsmod.remove_mount(conn, dom, "/nope", running=True)
    assert dom.detached == []
    assert conn.defined == []


def test_two_mounts_coexist_and_remove_one(env) -> None:
    conn, dom = env
    first = fsmod.add_mount(conn, dom, "/host/a", "/mnt/a", running=False)
    second = fsmod.add_mount(conn, dom, "/host/b", "/mnt/b", running=False)
    assert first.tag != second.tag

    mounts = fsmod.list_mounts(conn.defined[-1])
    assert {(m.target, m.source) for m in mounts} == {
        ("/mnt/a", "/host/a"),
        ("/mnt/b", "/host/b"),
    }

    fsmod.remove_mount(conn, dom, "/mnt/a", running=False)
    remaining = fsmod.list_mounts(conn.defined[-1])
    assert [m.target for m in remaining] == ["/mnt/b"]
    filesystem = ET.fromstring(conn.defined[-1]).find("./devices/filesystem")
    assert filesystem.find("target").get("dir") == second.tag
