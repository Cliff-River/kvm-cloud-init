"""domain XML 生成与启动（替代 virt-install --import --noautoconsole）。

virtinst 未发布到 PyPI，而它对 --import 场景做的事本质上就是拼装一份
domain XML 再调用 libvirt API；这里直接用 xml.etree 生成等价 XML。
UEFI 下显式写入无安全启动的普通 OVMF loader/NVRAM 模板（路径由
firmware.resolve_plain_uefi_pair 从固件描述符解析），避免 libvirt 自动
打分挑中 OVMF_CODE_4M.ms.fd 等带安全启动/预置证书的固件；未解析到路径
时只保留 firmware='efi'，交回 libvirt 自动选择。
"""

from __future__ import annotations

import xml.dom.minidom
import xml.etree.ElementTree as ET

import libvirt

from .osinfo import LIBOSINFO_NS, resolve_os_id

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
    loader: str | None = None,
    nvram_template: str | None = None,
) -> str:
    """生成与原 virt-install 参数等价的域 XML。

    ``loader``/``nvram_template`` 为普通（无安全启动）OVMF 的 CODE 与 VARS
    模板路径；仅在 firmware 为 efi 且二者齐全时写入，libvirt 会把模板复制
    为每实例独立的运行时 NVRAM。
    """
    ET.register_namespace("kci", METADATA_NS)
    ET.register_namespace("libosinfo", LIBOSINFO_NS)
    domain = ET.Element("domain", {"type": "kvm"})
    _sub(domain, "name", name)

    metadata = ET.SubElement(domain, "metadata")
    metadata.set("xmlns:kci", METADATA_NS)
    config = ET.SubElement(metadata, f"{{{METADATA_NS}}}config")
    _sub(config, f"{{{METADATA_NS}}}template", template_name)
    _sub(config, f"{{{METADATA_NS}}}os-variant", os_variant)

    os_id = resolve_os_id(os_variant)
    if os_id:
        libosinfo = ET.SubElement(metadata, f"{{{LIBOSINFO_NS}}}libosinfo")
        libosinfo.set("xmlns:libosinfo", LIBOSINFO_NS)
        _sub(libosinfo, f"{{{LIBOSINFO_NS}}}os", id=os_id)

    _sub(domain, "memory", str(memory_mib), unit="MiB")
    _sub(domain, "vcpu", str(vcpus), placement="static")

    os_el = _sub(domain, "os", firmware=firmware)
    _sub(os_el, "type", "hvm", arch="x86_64", machine="q35")
    if firmware == "efi" and loader and nvram_template:
        # 显式指定无安全启动的普通 OVMF，避免 libvirt 自动选中 ms/secboot 版
        _sub(os_el, "loader", loader, readonly="yes", type="pflash", format="raw")
        _sub(os_el, "nvram", template=nvram_template)
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
    dom = xml.dom.minidom.parseString(rough)
    # ElementTree 会把所有命名空间声明提升到根元素；这里把 kci/libosinfo 的声明
    # 留在各自的子元素上（metadata / libosinfo），移除根元素上的冗余副本。
    root = dom.documentElement
    if root.hasAttribute("xmlns:kci"):
        root.removeAttribute("xmlns:kci")
    if root.hasAttribute("xmlns:libosinfo"):
        root.removeAttribute("xmlns:libosinfo")
    return dom.toprettyxml(indent="  ")


def define_and_start(conn: libvirt.virConnect, xml_desc: str) -> libvirt.virDomain:
    """定义持久域并启动（不自动连接控制台，等价 --noautoconsole）。"""
    dom = conn.defineXML(xml_desc)
    dom.createWithFlags(0)
    return dom
