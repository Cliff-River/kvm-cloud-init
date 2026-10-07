"""用 pycdlib 生成 cloud-init NoCloud 种子 ISO（替代 genisoimage）。

ISO 特征必须与原 genisoimage 命令一致：
- 卷标 ``cidata``（NoCloud 数据源据此识别）
- 同时包含 ISO9660 / Joliet / RockRidge 名称，长文件名走 RR/Joliet
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pycdlib

VOLUME_LABEL = "cidata"


def _iso9660_name(name: str) -> str:
    """ISO9660 兼容名：大写、连字符转下划线、带版本号。"""
    return "/" + name.upper().replace("-", "_") + ";1"


def build_seed_iso(docs: dict[str, str], iso_path: Path) -> Path:
    """根据 {文档名: 内容} 生成 cidata.iso。

    文档先写入一个由调用方提供/管理的临时目录，再打包，因此本函数接收
    已落盘目录更便于测试；这里直接接收内容字典时使用内存外的临时文件。
    """
    iso = pycdlib.PyCdlib()
    try:
        iso.new(
            interchange_level=3,
            joliet=3,
            rock_ridge="1.12",
            vol_ident=VOLUME_LABEL,
        )
        with tempfile.TemporaryDirectory(prefix="kci-docs-") as staging:
            for name, content in docs.items():
                source = Path(staging) / name
                source.write_text(content, encoding="utf-8")
                iso.add_file(
                    str(source),
                    _iso9660_name(name),
                    rr_name=name,
                    joliet_path="/" + name,
                )
            iso.write(str(iso_path))
    finally:
        iso.close()
    return iso_path


@contextmanager
def temporary_seed(docs: dict[str, str]) -> Iterator[Path]:
    """在临时目录中生成 cidata.iso，退出上下文时自动清理。

    yield ISO 文件路径，供上传到 libvirt 存储池期间使用。
    """
    with tempfile.TemporaryDirectory(prefix="kci-seed-") as tmp:
        iso_path = Path(tmp) / "cidata.iso"
        build_seed_iso(docs, iso_path)
        yield iso_path
