"""libvirt 连接与存储池基础操作。

所有实例操作一律使用系统级会话 ``qemu:///system``，禁止使用 session 会话。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import libvirt

from ..errors import LibvirtConnectionError, ProvisionError

#: 唯一允许的连接目标
SYSTEM_URI = "qemu:///system"

#: virDomainState 数字 -> 人类可读状态
DOMAIN_STATE_NAMES: dict[int, str] = {
    libvirt.VIR_DOMAIN_NOSTATE: "nostate",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_SHUTOFF: "shut off",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}


# libvirt 默认会把每条错误（含我们主动捕获并处理的“不存在”类错误）
# 打印到 stderr；业务代码已自行处理异常，静默默认日志避免噪声。
libvirt.registerErrorHandler(lambda _ctx, _err: None, None)


def connect(uri: str = SYSTEM_URI) -> libvirt.virConnect:
    """打开 qemu:///system 连接，失败时抛出用户可读的错误。"""
    try:
        conn = libvirt.open(uri)
    except libvirt.libvirtError as exc:
        raise LibvirtConnectionError(
            f"无法连接 {uri}：{exc}。"
            "请确认 libvirtd 已运行（systemctl status libvirtd），"
            "且当前用户属于 libvirt 与 kvm 组（加入后需重新登录）。"
        ) from exc
    if conn is None:
        raise LibvirtConnectionError(f"无法连接 {uri}：libvirt.open 返回空连接")
    return conn


def get_pool(conn: libvirt.virConnect, name: str, refresh: bool = False) -> libvirt.virStoragePool:
    """查找存储池，必要时自动启动（构建/自动启动）。"""
    try:
        pool = conn.storagePoolLookupByName(name)
    except libvirt.libvirtError as exc:
        raise ProvisionError(f"找不到 libvirt 存储池 {name!r}：{exc}") from exc
    if pool.info()[0] != libvirt.VIR_STORAGE_POOL_RUNNING:
        pool.create(0)
    if refresh:
        pool.refresh(0)
    return pool


def pool_target_path(pool: libvirt.virStoragePool) -> str:
    """从存储池 XML 读取 target path。"""
    root = ET.fromstring(pool.XMLDesc(0))
    target = root.find("./target/path")
    if target is None or not target.text:
        raise ProvisionError("存储池 XML 中缺少 target/path")
    return target.text
