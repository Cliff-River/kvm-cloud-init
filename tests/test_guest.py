"""domain XML 生成测试。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kvm_cloud_init.hv.guest import METADATA_NS, build_domain_xml


def _build() -> str:
    return build_domain_xml(
        name="vm1",
        disk_path="/pool/vm1.qcow2",
        iso_path="/pool/vm1-cidata.iso",
        memory_mib=4096,
        vcpus=4,
        network="default",
        firmware="efi",
        os_variant="rocky10",
        template_name="Rocky-LLM",
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
        build_domain_xml(
            name="b", disk_path="/d.qcow2", iso_path="/c.iso",
            memory_mib=1, vcpus=1, network="n", firmware="bios",
            os_variant="debian13", template_name="t",
        )
    ).find("os")
    assert bios.get("firmware") == "bios"


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


def test_metadata() -> None:
    root = ET.fromstring(_build())
    config = root.find(f".//{{{METADATA_NS}}}config")
    assert config.findtext(f"{{{METADATA_NS}}}template") == "Rocky-LLM"
    assert config.findtext(f"{{{METADATA_NS}}}os-variant") == "rocky10"
