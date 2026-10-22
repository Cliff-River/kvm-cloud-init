"""domain XML 生成测试。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from unittest.mock import patch

from kvm_cloud_init.hv.guest import (
    LIBOSINFO_NS,
    METADATA_NS,
    build_domain_xml,
    domain_ssh_user,
)

# guest 测试只关心 XML 生成，osinfo 解析逻辑由 test_osinfo 覆盖；
# 这里固定 resolve_os_id 的返回，避免触发本机扫描与网络请求。
_FAKE_IDS = {
    "rocky10": "http://rockylinux.org/rocky/10",
    "ubuntu26.04": "http://ubuntu.com/ubuntu/26.04",
    "debian13": "http://debian.org/debian/13",
    "almalinux10": "http://almalinux.org/almalinux/10",
    "centos9": "http://centos.org/centos/9",
}


def _fake_resolve(short_id: str) -> str | None:
    return _FAKE_IDS.get(short_id)


def _build_xml(**kwargs) -> str:
    with patch("kvm_cloud_init.hv.guest.resolve_os_id", side_effect=_fake_resolve):
        return build_domain_xml(**kwargs)


def _build() -> str:
    return _build_xml(
        name="vm1",
        disk_path="/pool/vm1.qcow2",
        iso_path="/pool/vm1-cidata.iso",
        memory_mib=4096,
        vcpus=4,
        network="default",
        firmware="efi",
        os_variant="rocky10",
        template_name="Rocky-LVM",
    )


def test_basic_domain_xml() -> None:
    root = ET.fromstring(_build())
    assert root.get("type") == "kvm"
    assert root.findtext("name") == "vm1"
    assert root.findtext("memory") == "4096"
    assert root.find("memory").get("unit") == "MiB"
    assert root.findtext("vcpu") == "4"


def test_firmware_and_boot() -> None:
    os_el = ET.fromstring(_build()).find("os")
    assert os_el.get("firmware") == "efi"
    assert os_el.findtext("type") == "hvm"
    assert os_el.find("type").get("machine") == "q35"
    assert os_el.find("boot").get("dev") == "hd"

    bios = ET.fromstring(
        _build_xml(
            name="b", disk_path="/d.qcow2", iso_path="/c.iso",
            memory_mib=1, vcpus=1, network="n", firmware="bios",
            os_variant="debian13", template_name="t",
        )
    ).find("os")
    assert bios.get("firmware") == "bios"


def test_uefi_without_pair_omits_loader() -> None:
    # 未解析到普通 OVMF 时不写 loader/nvram，交回 libvirt 自动选择
    os_el = ET.fromstring(_build()).find("os")
    assert os_el.find("loader") is None
    assert os_el.find("nvram") is None


def test_uefi_explicit_plain_loader_nvram() -> None:
    root = _build_with(
        loader="/usr/share/OVMF/OVMF_CODE_4M.fd",
        nvram_template="/usr/share/OVMF/OVMF_VARS_4M.fd",
    )
    os_el = root.find("os")
    loader = os_el.find("loader")
    nvram = os_el.find("nvram")
    assert loader is not None
    assert loader.text == "/usr/share/OVMF/OVMF_CODE_4M.fd"
    assert loader.get("readonly") == "yes"
    assert loader.get("type") == "pflash"
    assert loader.get("format") == "raw"
    assert nvram is not None
    assert nvram.get("template") == "/usr/share/OVMF/OVMF_VARS_4M.fd"
    # 元素顺序必须为 type -> loader -> nvram -> boot
    tags = [child.tag for child in os_el]
    assert tags.index("type") < tags.index("loader") < tags.index("nvram")
    assert tags.index("nvram") < tags.index("boot")


def test_bios_ignores_loader_pair() -> None:
    root = _build_with(
        firmware="bios",
        loader="/usr/share/OVMF/OVMF_CODE_4M.fd",
        nvram_template="/usr/share/OVMF/OVMF_VARS_4M.fd",
    )
    os_el = root.find("os")
    assert os_el.find("loader") is None
    assert os_el.find("nvram") is None


def test_devices() -> None:
    root = ET.fromstring(_build())
    disks = root.findall("./devices/disk")
    assert len(disks) == 2

    system_disk, cdrom = disks
    assert system_disk.get("device") == "disk"
    assert system_disk.find("driver").get("type") == "qcow2"
    assert system_disk.find("source").get("file") == "/pool/vm1.qcow2"
    assert system_disk.find("target").get("dev") == "vda"
    assert system_disk.find("target").get("bus") == "virtio"

    assert cdrom.get("device") == "cdrom"
    assert cdrom.find("source").get("file") == "/pool/vm1-cidata.iso"
    assert cdrom.find("target").get("bus") == "sata"
    assert cdrom.find("readonly") is not None

    iface = root.find("./devices/interface")
    assert iface.get("type") == "network"
    assert iface.find("source").get("network") == "default"
    assert iface.find("model").get("type") == "virtio"

    assert root.find("./devices/serial") is not None
    assert root.find("./devices/console") is not None

    # 默认：VNC 图形控制台，auto 不生成 video 元素
    graphics = root.find("./devices/graphics")
    assert graphics.get("type") == "vnc"
    assert graphics.get("autoport") == "yes"
    assert root.find("./devices/video") is None


def _build_with(**kwargs) -> ET.Element:
    defaults = {
        "name": "vm1",
        "disk_path": "/pool/vm1.qcow2",
        "iso_path": "/pool/vm1-cidata.iso",
        "memory_mib": 4096,
        "vcpus": 4,
        "network": "default",
        "firmware": "efi",
        "os_variant": "rocky10",
        "template_name": "Rocky-LVM",
    }
    defaults.update(kwargs)
    return ET.fromstring(_build_xml(**defaults))


def test_graphics_spice() -> None:
    root = _build_with(graphics="spice")
    graphics = root.find("./devices/graphics")
    assert graphics.get("type") == "spice"
    assert graphics.get("autoport") == "yes"


def test_graphics_none_omits_device() -> None:
    root = _build_with(graphics="none")
    assert root.find("./devices/graphics") is None


def test_video_explicit_model() -> None:
    root = _build_with(video="virtio")
    model = root.find("./devices/video/model")
    assert model.get("type") == "virtio"
    assert model.get("heads") == "1"


def test_metadata() -> None:
    root = ET.fromstring(_build())
    config = root.find(f".//{{{METADATA_NS}}}config")
    assert config.findtext(f"{{{METADATA_NS}}}template") == "Rocky-LVM"
    assert config.findtext(f"{{{METADATA_NS}}}os-variant") == "rocky10"


def test_metadata_libosinfo_rocky() -> None:
    xml_str = _build()
    # xmlns:libosinfo 声明必须落在 libosinfo 元素自身上
    assert (
        '<libosinfo:libosinfo xmlns:libosinfo='
        '"http://libosinfo.org/xmlns/libvirt/domain/1.0">'
    ) in xml_str
    root = ET.fromstring(xml_str)
    libosinfo = root.find(f".//{{{LIBOSINFO_NS}}}libosinfo")
    assert libosinfo is not None
    os_el = libosinfo.find(f"{{{LIBOSINFO_NS}}}os")
    assert os_el.get("id") == "http://rockylinux.org/rocky/10"


def test_metadata_libosinfo_variants() -> None:
    variants = {
        "ubuntu26.04": "http://ubuntu.com/ubuntu/26.04",
        "debian13": "http://debian.org/debian/13",
        "almalinux10": "http://almalinux.org/almalinux/10",
    }
    for variant, expected in variants.items():
        root = _build_with(os_variant=variant)
        os_el = root.find(f".//{{{LIBOSINFO_NS}}}libosinfo/{{{LIBOSINFO_NS}}}os")
        assert os_el.get("id") == expected


def test_metadata_libosinfo_derived() -> None:
    # 未登记的 variant 走发行版前缀推导
    root = _build_with(os_variant="centos9")
    os_el = root.find(f".//{{{LIBOSINFO_NS}}}libosinfo/{{{LIBOSINFO_NS}}}os")
    assert os_el.get("id") == "http://centos.org/centos/9"


def test_metadata_libosinfo_unknown_omitted() -> None:
    # 无法识别的 os-variant 不生成 libosinfo 节点
    root = _build_with(os_variant="totally-unknown-99")
    assert root.find(f".//{{{LIBOSINFO_NS}}}libosinfo") is None


def test_metadata_ssh_user_written() -> None:
    config = _build_with(ssh_user="cliff").find(f".//{{{METADATA_NS}}}config")
    assert config.findtext(f"{{{METADATA_NS}}}ssh-user") == "cliff"


def test_metadata_ssh_user_omitted_by_default() -> None:
    config = ET.fromstring(_build()).find(f".//{{{METADATA_NS}}}config")
    assert config.find(f"{{{METADATA_NS}}}ssh-user") is None


class _FakeDom:
    def __init__(self, xml: str) -> None:
        self._xml = xml

    def XMLDesc(self, _flags: int) -> str:
        return self._xml


def test_domain_ssh_user_reader() -> None:
    with_ssh = _build_xml(
        name="vm1", disk_path="/d.qcow2", iso_path="/c.iso", memory_mib=1,
        vcpus=1, network="default", firmware="bios", os_variant="rocky10",
        template_name="t", ssh_user="cliff",
    )
    assert domain_ssh_user(_FakeDom(with_ssh)) == "cliff"
    assert domain_ssh_user(_FakeDom(_build())) is None
