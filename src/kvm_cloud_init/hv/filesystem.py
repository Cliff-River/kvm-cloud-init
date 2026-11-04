"""domain 文件系统共享设备（9p）的热插拔与元数据管理。

``mount``/``unmount`` 子命令在持久域 XML 中维护一组 9p filesystem 设备，
并在实例运行时通过 libvirt 实时 attach/detach；挂载点信息记录在 kci 元数据
``<kci:mounts>`` 中（不新增任何项目侧状态文件）。

9p 设备的 ``target dir`` 是 **mount tag**（guest 内 ``mount -t 9p <tag>``
使用的标识），不是 guest 内挂载路径，两者分别保存在 tag 与 target 字段。
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import libvirt

from ..errors import MountError
from .guest import METADATA_NS
from .osinfo import LIBOSINFO_NS

#: 重新序列化持久域 XML 时保持既有前缀，避免生成 ns0/ns1
ET.register_namespace("kci", METADATA_NS)
ET.register_namespace("libosinfo", LIBOSINFO_NS)

#: 第一版只支持 9p（QEMU 内置，无需额外守护进程；virtiofs 留给后续）
BACKEND = "9p"


@dataclass(frozen=True)
class MountInfo:
    """一条已登记的共享挂载。"""

    tag: str
    source: str
    target: str


def mount_tag(instance: str, target: str) -> str:
    """由实例名与 guest 挂载点确定性生成 9p mount tag。"""
    digest = hashlib.sha1(f"{instance}\0{target}".encode()).hexdigest()
    return f"kci_{digest[:12]}"


def build_filesystem_xml(source: str, tag: str) -> str:
    """生成 9p filesystem 设备 XML 片段（供 attach/detach 使用）。"""
    filesystem = ET.Element(
        "filesystem", {"type": "mount", "accessmode": "passthrough"}
    )
    ET.SubElement(filesystem, "driver", {"type": "path", "wrpolicy": "immediate"})
    ET.SubElement(filesystem, "source", {"dir": source})
    ET.SubElement(filesystem, "target", {"dir": tag})
    return ET.tostring(filesystem, encoding="unicode")


def list_mounts(xml_desc: str) -> list[MountInfo]:
    """从域 XML 的 kci 元数据读取全部已登记挂载。"""
    root = ET.fromstring(xml_desc)
    mounts: list[MountInfo] = []
    for element in root.findall(f".//{{{METADATA_NS}}}mount"):
        tag = element.get("tag")
        source = element.get("source")
        target = element.get("target")
        if tag and source and target:
            mounts.append(MountInfo(tag=tag, source=source, target=target))
    return mounts


def find_mount(xml_desc: str, target: str) -> MountInfo | None:
    """按 guest 挂载点查找已登记挂载。"""
    return next(
        (mount for mount in list_mounts(xml_desc) if mount.target == target),
        None,
    )


def _config_element(root: ET.Element) -> ET.Element:
    config = root.find(f".//{{{METADATA_NS}}}config")
    if config is None:
        raise MountError("Domain is missing the kci:config metadata element")
    return config


def add_mount(
    conn: libvirt.virConnect,
    dom: libvirt.virDomain,
    source: str,
    target: str,
    *,
    running: bool,
) -> MountInfo:
    """在持久域配置中登记并（运行时）热插入 9p 设备，返回挂载信息。

    重复挂载点直接报错；实时 attach 失败时回滚持久配置，保证两侧一致。
    """
    original = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(original)
    name = root.findtext("name") or dom.name()
    if find_mount(original, target) is not None:
        raise MountError(f"{target} is already mounted in instance {name}")

    devices = root.find("./devices")
    if devices is None:
        raise MountError(f"Domain {name} has no devices section in its XML")

    info = MountInfo(tag=mount_tag(name, target), source=source, target=target)
    devices.append(ET.fromstring(build_filesystem_xml(info.source, info.tag)))

    config = _config_element(root)
    mounts = config.find(f"{{{METADATA_NS}}}mounts")
    if mounts is None:
        mounts = ET.SubElement(config, f"{{{METADATA_NS}}}mounts")
    ET.SubElement(
        mounts,
        f"{{{METADATA_NS}}}mount",
        {"tag": info.tag, "source": info.source, "target": info.target},
    )

    conn.defineXML(ET.tostring(root, encoding="unicode"))
    if running:
        try:
            dom.attachDeviceFlags(build_filesystem_xml(source, info.tag), 0)
        except libvirt.libvirtError as exc:
            # 热插失败：回滚持久配置，避免“已登记但 guest 看不到设备”的悬挂状态
            conn.defineXML(original)
            raise MountError(
                f"Cannot attach the 9p filesystem device to running instance "
                f"{name}: {exc}"
            ) from exc
    return info


def remove_mount(
    conn: libvirt.virConnect,
    dom: libvirt.virDomain,
    target: str,
    *,
    running: bool,
) -> MountInfo:
    """从持久域配置与运行态移除指定挂载点，返回被移除的挂载信息。"""
    original = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(original)
    name = root.findtext("name") or dom.name()
    info = find_mount(original, target)
    if info is None:
        raise MountError(f"No mounted host directory found at {target} in {name}")

    devices = root.find("./devices")
    if devices is not None:
        for filesystem in devices.findall("filesystem"):
            target_el = filesystem.find("target")
            if target_el is not None and target_el.get("dir") == info.tag:
                devices.remove(filesystem)
                break

    config = _config_element(root)
    mounts = config.find(f"{{{METADATA_NS}}}mounts")
    if mounts is not None:
        for mount_el in list(mounts.findall(f"{{{METADATA_NS}}}mount")):
            if mount_el.get("target") == target:
                mounts.remove(mount_el)
        if not list(mounts):
            config.remove(mounts)

    # 先从运行态摘除（失败则保留持久配置不动），再更新持久配置
    if running:
        try:
            dom.detachDeviceFlags(build_filesystem_xml(info.source, info.tag), 0)
        except libvirt.libvirtError as exc:
            raise MountError(
                f"Cannot detach the 9p filesystem device from running instance "
                f"{name}: {exc}"
            ) from exc
    conn.defineXML(ET.tostring(root, encoding="unicode"))
    return info
