"""UEFI/BIOS 固件选择。

优先使用 libvirt 域能力 XML（与 libvirt 自动选固件同一信息源），
域能力不可用时回退到文件系统扫描（固件描述符 JSON、常见 OVMF 路径）。
"""

from __future__ import annotations

import glob
import xml.etree.ElementTree as ET

import libvirt

_OVMF_GLOBS = (
    "/usr/share/OVMF/OVMF_CODE*.fd",
    "/usr/share/edk2/ovmf/OVMF_CODE*.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_CODE*.fd",
    "/usr/share/qemu/ovmf-x86_64-code.bin",
)
_FIRMWARE_DESC_GLOBS = (
    "/usr/share/qemu/firmware/*.json",
    "/etc/qemu/firmware/*.json",
)


def _uefi_from_domain_caps(conn: libvirt.virConnect) -> bool | None:
    """解析域能力 XML 中 enum[name=firmware] 是否包含 efi。"""
    for args in ((None, "x86_64", None, "kvm"), ("", "x86_64", "", "kvm")):
        try:
            xml = conn.getDomainCapabilities(*args)
        except libvirt.libvirtError:
            continue
        except TypeError:
            continue
        if not xml:
            continue
        root = ET.fromstring(xml)
        enum = root.find("./os/enum[@name='firmware']")
        if enum is None:
            return False
        return any(value.text == "efi" for value in enum.findall("./value"))
    return None


def _uefi_from_filesystem() -> bool:
    for pattern in _FIRMWARE_DESC_GLOBS:
        for path in glob.glob(pattern):
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if "uefi" in text.lower():
                return True
    return any(glob.glob(pattern) for pattern in _OVMF_GLOBS)


def uefi_available(conn: libvirt.virConnect) -> bool:
    """主机是否具备可用的 UEFI 固件。"""
    detected = _uefi_from_domain_caps(conn)
    if detected is not None:
        return detected
    return _uefi_from_filesystem()


def choose_firmware(policy: str, conn: libvirt.virConnect) -> str:
    """把配置策略（auto/uefi/bios）翻译成 domain XML 的 firmware 值。"""
    if policy == "bios":
        return "bios"
    if policy == "uefi":
        return "efi"
    return "efi" if uefi_available(conn) else "bios"
