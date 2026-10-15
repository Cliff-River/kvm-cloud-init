"""UEFI/BIOS 固件选择。

优先使用 libvirt 域能力 XML（与 libvirt 自动选固件同一信息源），
域能力不可用时回退到文件系统扫描（固件描述符 JSON、常见 OVMF 路径）。

选用 UEFI 时不裸写 ``<os firmware='efi'>`` 交给 libvirt 自由打分——在部分
发行版上它会挑中带安全启动/预置证书或 SEV 的固件（如 OVMF_CODE_4M.ms.fd），
用于普通 Linux 客户机可能无法引导。``resolve_plain_uefi_pair`` 从同一份固件
描述符 JSON 中挑出无安全启动的普通 pflash OVMF（CODE + VARS 模板），由
guest.py 显式写入 loader/nvram；描述符缺失时再退回 libvirt 自动选择。
"""

from __future__ import annotations

import glob
import json
import os
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


#: 普通 Linux 客户机排除的固件特性：安全启动及其预置证书、SEV/TDX 专用固件
_PLAIN_UEFI_EXCLUDED_FEATURES = frozenset(
    ("secure-boot", "enrolled-keys", "requires-smm", "amd-sev-es", "amd-sev-snp", "intel-tdx")
)


def _load_firmware_descriptors() -> list[dict]:
    """读取 qemu 固件描述符 JSON（与 libvirt 的查找目录一致）。"""
    descriptors: list[dict] = []
    seen: set[str] = set()
    for pattern in _FIRMWARE_DESC_GLOBS:
        for path in sorted(glob.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                descriptors.append(data)
    return descriptors


def resolve_plain_uefi_pair() -> tuple[str, str] | None:
    """挑选无安全启动的普通 pflash OVMF，返回 (CODE, VARS 模板) 路径。

    依据固件描述符 JSON 选择：interface 为 uefi、flash 设备且带 nvram 模板、
    面向 x86_64/q35、不含安全启动/SEV 等特性，且两个文件在本机真实存在。
    多个候选时取特性最少者（确定性选择）。找不到则返回 None，调用方退回
    libvirt 自动选择。
    """
    candidates: list[tuple[int, str, str]] = []
    for desc in _load_firmware_descriptors():
        mapping = desc.get("mapping")
        if not isinstance(mapping, dict) or mapping.get("device") != "flash":
            continue
        if "uefi" not in desc.get("interface-types", ()):
            continue
        features = frozenset(desc.get("features", ()))
        if features & _PLAIN_UEFI_EXCLUDED_FEATURES:
            continue
        executable = mapping.get("executable")
        nvram = mapping.get("nvram-template")
        if not isinstance(executable, dict) or not isinstance(nvram, dict):
            continue
        code = executable.get("filename")
        vars_template = nvram.get("filename")
        if not isinstance(code, str) or not isinstance(vars_template, str):
            continue
        if not any(
            target.get("architecture") == "x86_64"
            and any("q35" in str(machine) for machine in target.get("machines", ()))
            for target in desc.get("targets", ())
            if isinstance(target, dict)
        ):
            continue
        if not (os.path.exists(code) and os.path.exists(vars_template)):
            continue
        candidates.append((len(features), code, vars_template))

    if not candidates:
        return None
    candidates.sort()
    _, code, vars_template = candidates[0]
    return code, vars_template
