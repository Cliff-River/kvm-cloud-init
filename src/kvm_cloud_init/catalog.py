"""内置主流发行版云镜像预设，以及 ``images.conf`` 的确定性渲染。

本模块是纯数据 + 纯字符串处理，不触网、不读配置文件，因此可以安全地被
``config.py`` 在 ``images.conf`` 缺失时调用。

预设按家族定义（版本号作为参数），覆盖 AlmaLinux / Rocky Linux /
CentOS Stream / Debian / Ubuntu / Fedora 的主版本；新增家族只需在此追加
条目，用户自定义源则写入 ``images.user.conf``，无需改代码。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

CATALOG_FILENAME = "images.conf"
USER_CATALOG_FILENAME = "images.user.conf"

HEADER = """\
# kvmci 云镜像目录（系统自动生成，请勿手工编辑）
#
# 本文件由内置预设渲染，执行 `kvmci images refresh` 可重建并刷新上游版本。
# 自定义镜像源请写入 images.user.conf：可新增条目，也可用同名条目覆盖本文件。
# 每个条目被 templates.conf 的 image 字段按 ID 引用。
#
# 字段说明：
#   file            下载后保存到 image_dir 的固定文件名
#   kind            url（固定直链）| index（目录页爬虫，按版本/日期取最新）
#   urls / pages    url 的直链候选 / index 的目录页候选，有序回退
#   match           index 模式下匹配 href 文件名的正则
#   order_by        name（默认，文件名字典序最大）| mtime（Last-Modified 最新）
#   checksum_url    url 模式的校验清单直链
#   checksum_file   index 模式下目录页内的校验清单文件名
#   checksum_match  index 模式下校验清单文件名的正则（清单名带版本时使用）
#   checksum_algo   sha256（默认）| sha512
#   insecure        true 时允许无校验和下载；默认 false
#   archived        EOL 版本，check 默认跳过
"""


@dataclass(frozen=True)
class PresetEntry:
    """一条内置镜像预设；渲染成 images.conf 的一个顶层键。"""

    id: str
    file: str
    kind: str
    urls: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    match: str = ""
    checksum_url: str = ""
    checksum_file: str = ""
    checksum_match: str = ""
    checksum_algo: str = "sha256"
    archived: bool = False
    arch: str = "x86_64"
    notes: tuple[str, ...] = field(default=())


def _almalinux(version: str) -> PresetEntry:
    base = f"AlmaLinux-{version}-GenericCloud"
    return PresetEntry(
        id=f"almalinux-{version}",
        file=f"{base}.x86_64.qcow2",
        kind="index",
        pages=(
            f"https://repo.almalinux.org/almalinux/{version}/cloud/x86_64/images/",
        ),
        match=rf"{base}-\d+\.\d+-\d{{8}}(?:\.\d+)?\.x86_64\.qcow2$",
        checksum_file="CHECKSUM",
    )


def _rocky(version: str, variant: str) -> PresetEntry:
    # 上游 base 变体文件名带 -Base 段（Rocky-9-GenericCloud-Base-9.8-...），
    # LVM 不带；本地卷名沿用不带变体段的短名。
    remote_variant = "Base" if variant == "base" else variant.upper()
    remote = f"Rocky-{version}-GenericCloud-{remote_variant}"
    local = f"Rocky-{version}-GenericCloud" + (
        "" if variant == "base" else f"-{remote_variant}"
    )
    return PresetEntry(
        id=f"rocky-{version}" + ("" if variant == "base" else f"-{variant}"),
        file=f"{local}.x86_64.qcow2",
        kind="index",
        pages=(
            f"https://download.rockylinux.org/pub/rocky/{version}/images/x86_64/",
        ),
        match=rf"{remote}-\d+\.\d+-\d{{8}}\.\d+\.x86_64\.qcow2$",
        checksum_file="CHECKSUM",
        archived=version == "8",
    )


def _centos_stream(version: str) -> PresetEntry:
    stem = f"CentOS-Stream-GenericCloud-{version}"
    return PresetEntry(
        id=f"centos-stream-{version}",
        file=f"{stem}.x86_64.qcow2",
        kind="index",
        pages=(
            f"https://cloud.centos.org/centos/{version}-stream/x86_64/images/",
        ),
        # CentOS 同时存在旧命名（...-x86_64-9-<date>），正则只取新命名。
        match=rf"{stem}-\d{{8}}\.\d+\.x86_64\.qcow2$",
        checksum_file="CHECKSUM",
    )


def _debian(version: str, codename: str, archived: bool) -> PresetEntry:
    return PresetEntry(
        id=f"debian-{version}",
        file=f"debian-{version}-generic-amd64.qcow2",
        kind="index",
        pages=(
            f"https://cloud.debian.org/images/cloud/{codename}/latest/",
        ),
        match=rf"debian-{version}-generic-amd64\.qcow2$",
        checksum_file="SHA512SUMS",
        checksum_algo="sha512",
        archived=archived,
    )


def _ubuntu(version: str, archived: bool, variant: str = "amd64") -> PresetEntry:
    stem = f"ubuntu-{version}-server-cloudimg-{variant}"
    return PresetEntry(
        id=f"ubuntu-{version}",
        file=f"{stem}.qcow2",
        kind="url",
        urls=(
            f"https://cloud-images.ubuntu.com/releases/{version}/release/{stem}.img",
        ),
        checksum_url=(
            f"https://cloud-images.ubuntu.com/releases/{version}/release/SHA256SUMS"
        ),
        archived=archived,
    )


def _fedora(version: str) -> PresetEntry:
    stem = f"Fedora-Cloud-Base-Generic-{version}"
    return PresetEntry(
        id=f"fedora-{version}",
        file=f"{stem}.x86_64.qcow2",
        kind="index",
        pages=(
            f"https://download.fedoraproject.org/pub/fedora/linux/releases/{version}/Cloud/x86_64/images/",
        ),
        match=rf"{stem}-\d+\.\d+\.x86_64\.qcow2$",
        checksum_match=rf"Fedora-Cloud-{version}-[\d.]+-x86_64-CHECKSUM$",
    )


#: 内置镜像预设；ID 命名规则为 ``<家族>-<版本>[-变体]``，保持稳定可预测。
PRESET: tuple[PresetEntry, ...] = (
    _almalinux("8"),
    _almalinux("9"),
    _almalinux("10"),
    _rocky("8", "base"),
    _rocky("8", "lvm"),
    _rocky("9", "base"),
    _rocky("9", "lvm"),
    _rocky("10", "base"),
    _rocky("10", "lvm"),
    _centos_stream("9"),
    _centos_stream("10"),
    _debian("11", "bullseye", archived=True),
    _debian("12", "bookworm", archived=False),
    _debian("13", "trixie", archived=False),
    _ubuntu("20.04", archived=True),
    _ubuntu("22.04", archived=False),
    _ubuntu("24.04", archived=False),
    _ubuntu("26.04", archived=False, variant="amd64v3"),
    _fedora("43"),
    _fedora("44"),
)


def preset_ids() -> tuple[str, ...]:
    """返回全部内置镜像 ID（按预设顺序）。"""
    return tuple(entry.id for entry in PRESET)


def render_catalog(probes: Mapping[str, Mapping[str, str]] | None = None) -> str:
    """把内置预设渲染为 ``images.conf`` 文本。

    ``probes`` 为每个 ID 的上游探测结果（``upstream_file`` /
    ``upstream_version`` / ``checked_at`` / ``upstream_status``），刷新时写入
    生成文件；不提供时只渲染静态定义。输出是确定性的：同样的输入必然得到
    逐字节相同的文本。
    """
    lines = [HEADER.rstrip("\n"), ""]
    for entry in PRESET:
        lines.append(f"{entry.id}:")
        lines.append(f"  file: {entry.file}")
        lines.append(f"  kind: {entry.kind}")
        lines.append(f"  arch: {entry.arch}")
        if entry.urls:
            lines.append("  urls:")
            lines.extend(f"    - {url}" for url in entry.urls)
        if entry.pages:
            lines.append("  pages:")
            lines.extend(f"    - {url}" for url in entry.pages)
        if entry.match:
            lines.append(f"  match: '{entry.match}'")
        if entry.checksum_url:
            lines.append(f"  checksum_url: {entry.checksum_url}")
        if entry.checksum_file:
            lines.append(f"  checksum_file: {entry.checksum_file}")
        if entry.checksum_match:
            lines.append(f"  checksum_match: '{entry.checksum_match}'")
        lines.append(f"  checksum_algo: {entry.checksum_algo}")
        if entry.archived:
            lines.append("  archived: true")
        probe = (probes or {}).get(entry.id)
        if probe:
            lines.append("  # 上次 refresh 的上游探测结果")
            for key in ("upstream_file", "upstream_version", "checked_at", "upstream_status"):
                value = probe.get(key)
                if value:
                    lines.append(f"  {key}: {value}")
        lines.append("")
    return "\n".join(lines)
