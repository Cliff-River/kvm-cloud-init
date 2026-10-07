"""domain XML 生成与启动（替代 virt-install --import --noautoconsole）。

virtinst 未发布到 PyPI，而它对 --import 场景做的事本质上就是拼装一份
domain XML 再调用 libvirt API；这里直接用 xml.etree 生成等价 XML，
firmware='efi' 交给 libvirt 按固件描述符自动选择 OVMF 与 NVRAM 模板。
"""

from __future__ import annotations

import xml.dom.minidom
import xml.etree.ElementTree as ET

import libvirt

METADATA_NS = "https://raw.githubusercontent.com/kvm-cloud-init/schema/v1"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        element.text = text
    return element


def build_domain_xml(
    name: str,
    disk_path: str,
    iso_path: str,
    memory_mib: int,
    vcpus: int,
    network: str,
    firmware: str,
    os_variant: str,
    template_name: str,
    graphics: str = "vnc",
    video: str = "auto",
) -> str:
    """生成与原 virt-install 参数等价的域 XML。"""
    ET.register_namespace("kci", METADATA_NS)
    domain = ET.Element("domain", {"type": "kvm"})
    _sub(domain, "name", name)

    metadata = ET.SubElement(domain, "metadata")
    config = ET.SubElement(metadata, f"{{{METADATA_NS}}}config")
    _sub(config, f"{{{METADATA_NS}}}template", template_name)
    _sub(config, f"{{{METADATA_NS}}}os-variant", os_variant)

    _sub(domain, "memory", str(memory_mib), unit="MiB")
    _sub(domain, "vcpu", str(vcpus), placement="static")

    os_el = _sub(domain, "os", firmware=firmware)
    _sub(os_el, "type", "hvm", arch="x86_64", machine="q35")
    _sub(os_el, "boot", dev="hd")

    features = _sub(domain, "features")
    _sub(features, "acpi")
    _sub(features, "apic")

    _sub(domain, "cpu", mode="host-passthrough", check="none")
    clock = _sub(domain, "clock", offset="utc")
    _sub(clock, "timer", name="kvmclock")
    _sub(clock, "timer", name="rtc", tickpolicy="catchup")

    _sub(domain, "on_poweroff", "destroy")
    _sub(domain, "on_reboot", "restart")
    _sub(domain, "on_crash", "destroy")

    devices = _sub(domain, "devices")

    disk = _sub(devices, "disk", type="file", device="disk")
    _sub(disk, "driver", name="qemu", type="qcow2")
    _sub(disk, "source", file=disk_path)
    _sub(disk, "target", dev="vda", bus="virtio")

    cdrom = _sub(devices, "disk", type="file", device="cdrom")
    _sub(cdrom, "driver", name="qemu", type="raw")
    _sub(cdrom, "source", file=iso_path)
    _sub(cdrom, "target", dev="sda", bus="sata")
    _sub(cdrom, "readonly")

    interface = _sub(devices, "interface", type="network")
    _sub(interface, "source", network=network)
    _sub(interface, "model", type="virtio")

    serial = _sub(devices, "serial", type="pty")
    _sub(serial, "target", type="isa-serial", port="0")
    console = _sub(devices, "console", type="pty")
    _sub(console, "target", type="serial", port="0")

    # 图形控制台：vnc/spice 自动选端口；none 表示无图形设备（仅串口）
    if graphics == "vnc":
        _sub(devices, "graphics", type="vnc", port="-1", autoport="yes")
    elif graphics == "spice":
        _sub(devices, "graphics", type="spice", autoport="yes")

    # 虚拟显卡：auto 不写 video 元素，由 libvirt/qemu 采用其默认型号
    if video != "auto":
        video_el = _sub(devices, "video")
        _sub(video_el, "model", type=video, heads="1")

    _sub(devices, "memballoon", model="virtio")
    rng = _sub(devices, "rng", model="virtio")
    _sub(rng, "backend", "/dev/urandom", model="random")

    rough = ET.tostring(domain, encoding="unicode")
    return xml.dom.minidom.parseString(rough).toprettyxml(indent="  ")


def define_and_start(conn: libvirt.virConnect, xml_desc: str) -> libvirt.virDomain:
    """定义持久域并启动（不自动连接控制台，等价 --noautoconsole）。"""
    dom = conn.defineXML(xml_desc)
    dom.createWithFlags(0)
    return dom
