"""根据 os-variant(short-id) 解析 libosinfo OS ID。

libosinfo 官方不提供 REST Web API，但其数据库（osinfo-db）本身是公开的：
  - 本机已安装的 osinfo-db XML（osinfo-query 读取的同一份数据）
  - 官方 GitLab 仓库 https://gitlab.com/libosinfo/osinfo-db 的原始 XML

解析优先级：本机 osinfo-db > 官方仓库在线拉取 > 内置映射 > 前缀推导兜底。
全程使用 xml.etree 解析，不依赖 subprocess 或 GI 绑定。
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# libvirt domain metadata 中 libosinfo 节点的命名空间（与 OS XML 无关）。
LIBOSINFO_NS = "http://libosinfo.org/xmlns/libvirt/domain/1.0"

# osinfo-db 标准搜索路径，与 libosinfo 库默认路径一致。
_OSINFO_OS_PATHS: tuple[Path, ...] = (
    Path("/usr/share/osinfo/os"),
    Path("/etc/osinfo/os"),
    Path("/var/lib/osinfo/os"),
    Path(os.path.expanduser("~/.local/share/osinfo/os")),
)

# 官方 osinfo-db 仓库原始文件 URL 前缀。
_GITLAB_RAW = "https://gitlab.com/libosinfo/osinfo-db/-/raw/main/data/os"

# 发行版前缀 -> (仓库中的厂商目录名, 产品名)，用于构造在线文件路径与推导兜底。
_DISTRO_ROOTS: dict[str, tuple[str, str]] = {
    "rocky": ("rockylinux.org", "rocky"),
    "ubuntu": ("ubuntu.com", "ubuntu"),
    "debian": ("debian.org", "debian"),
    "almalinux": ("almalinux.org", "almalinux"),
    "centos": ("centos.org", "centos"),
    "fedora": ("fedoraproject.org", "fedora"),
    "opensuse": ("opensuse.org", "opensuse"),
    "archlinux": ("archlinux.org", "archlinux"),
}

# 内置兜底映射，覆盖本机/网络均不可用时的已知模板。
_BUILTIN_IDS: dict[str, str] = {
    "rocky10": "http://rockylinux.org/rocky/10",
    "ubuntu26.04": "http://ubuntu.com/ubuntu/26.04",
    "debian13": "http://debian.org/debian/13",
    "almalinux10": "http://almalinux.org/almalinux/10",
}

# 进程内缓存：short-id -> os-id。None 表示尚未加载本机数据库。
_cache: dict[str, str] | None = None
_local_loaded = False


def _parse_os_xml(data: bytes | str) -> tuple[str, str] | None:
    """从 osinfo-db 的 OS XML 中提取 (short-id, id)。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    os_el = root if root.tag == "os" else root.find("os")
    if os_el is None:
        return None
    os_id = os_el.get("id")
    short_id = os_el.findtext("short-id")
    if os_id and short_id:
        return short_id, os_id
    return None


def _load_local_db() -> dict[str, str]:
    """扫描本机 osinfo-db 目录，构建 short-id -> os-id 映射。"""
    mapping: dict[str, str] = {}
    for base in _OSINFO_OS_PATHS:
        if not base.is_dir():
            continue
        for xml_file in base.rglob("*.xml"):
            try:
                result = _parse_os_xml(xml_file.read_bytes())
            except OSError:
                continue
            if result:
                short_id, os_id = result
                mapping.setdefault(short_id, os_id)
    return mapping


def _fetch_online(short_id: str) -> str | None:
    """从官方 osinfo-db GitLab 仓库拉取单个 OS 的 XML 并解析 id。"""
    for prefix, (vendor, product) in _DISTRO_ROOTS.items():
        if short_id.startswith(prefix):
            version = short_id[len(prefix):]
            if not version:
                continue
            url = f"{_GITLAB_RAW}/{vendor}/{product}-{version}.xml.in"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = resp.read()
            except (urllib.error.URLError, TimeoutError, OSError):
                return None
            result = _parse_os_xml(data)
            if result:
                return result[1]
    return None


def resolve_os_id(short_id: str) -> str | None:
    """根据 os-variant(short-id) 解析 libosinfo OS ID，无法识别返回 None。

    解析顺序：本机 osinfo-db -> 官方仓库在线拉取 -> 内置映射 -> 前缀推导。
    """
    global _cache, _local_loaded
    if _cache is None:
        _cache = {}
    if not _local_loaded:
        _cache.update(_load_local_db())
        _local_loaded = True

    if short_id in _cache:
        return _cache[short_id]

    online = _fetch_online(short_id)
    if online:
        _cache[short_id] = online
        return online

    if short_id in _BUILTIN_IDS:
        os_id = _BUILTIN_IDS[short_id]
        _cache[short_id] = os_id
        return os_id

    # 前缀推导兜底
    for prefix, (vendor, product) in _DISTRO_ROOTS.items():
        if short_id.startswith(prefix):
            version = short_id[len(prefix):]
            if version:
                os_id = f"http://{vendor}/{product}/{version}"
                _cache[short_id] = os_id
                return os_id
    return None


def reset_cache() -> None:
    """清空缓存，强制下次解析重新加载本机数据库。仅供测试使用。"""
    global _cache, _local_loaded
    _cache = None
    _local_loaded = False
