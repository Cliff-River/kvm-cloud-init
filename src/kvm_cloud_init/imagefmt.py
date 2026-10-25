"""镜像格式探测与 raw → qcow2 转换（纯标准库实现，不依赖 qemu-img）。

项目只涉及两种落盘格式：

- ``qcow2``：主流云镜像格式，可扩容、可快照；
- ``raw``：裸盘（含 Kali 的 ``disk.raw``），体积等于虚拟大小。

上游发布物常是压缩包（Kali 的 ``.tar.xz`` 里就是裸盘），解压后未必符合
``images.conf`` 声明的 ``format``，需要在此转换。转换只实现 raw → qcow2：
qcow2 是功能更完整的格式，反向转换没有实际用途。

生成的 qcow2 遵循 v3 规范（``docs/interop/qcow2.txt``）：固定 64 KiB 簇、
refcount order 4（16 位引用计数）、全零簇不分配，因此 25 GiB 裸盘转换后
通常只占实际数据大小。
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from .errors import ImageFetchError
from .qcow2 import QCOW2_MAGIC

CLUSTER_BITS = 16
CLUSTER_SIZE = 1 << CLUSTER_BITS
#: L2 表项数（每个表项 8 字节）与 refcount 表项数（每个表项 2 字节）
L2_ENTRIES = CLUSTER_SIZE // 8
REFCOUNT_PER_BLOCK = CLUSTER_SIZE // 2

FORMATS = ("qcow2", "raw")

_HEADER = struct.Struct(">4sIQIIQIIQQIIQQQQII")
_COPIED = 1 << 63
#: qemu 以 512 字节扇区为单位看待虚拟大小
_SECTOR_SIZE = 512

_ZERO_BLOCK = b"\x00" * CLUSTER_SIZE


def detect(path: str | Path) -> str:
    """按文件头判定格式：``QFI\\xfb`` 为 qcow2，其余按 raw 处理。"""
    with open(path, "rb") as fh:
        head = fh.read(4)
    return "qcow2" if head == QCOW2_MAGIC else "raw"


def convert(src: str | Path, dst: str | Path, fmt: str) -> None:
    """把 ``src`` 转换为 ``fmt`` 格式写入 ``dst``。

    只在源格式与目标格式不同时需要调用；qcow2 → raw 未实现，会明确报错。
    """
    if fmt not in FORMATS:
        raise ImageFetchError(f"Unsupported image format: {fmt}")
    actual = detect(src)
    if actual == fmt:
        raise ImageFetchError(
            f"Source image {src} is already {fmt}; conversion is not required"
        )
    if actual == "qcow2":
        raise ImageFetchError(
            "Converting qcow2 to raw is not supported; use format: qcow2 instead"
        )
    _raw_to_qcow2(Path(src), Path(dst), os.path.getsize(src))


def _raw_to_qcow2(src: Path, dst: Path, size: int) -> None:
    """流式把裸盘写成 qcow2：全零簇跳过，元数据在数据之后追加。"""
    if size <= 0:
        raise ImageFetchError(f"Source image is empty: {src}")
    # qemu 按 512 字节扇区向下取整虚拟大小，向上补齐才能如实还原
    virtual_size = (size + _SECTOR_SIZE - 1) // _SECTOR_SIZE * _SECTOR_SIZE
    clusters = _clusters_for(virtual_size)
    l1_size = max(1, (clusters + L2_ENTRIES - 1) // L2_ENTRIES)
    l2_offsets: list[int | None] = [None] * l1_size

    with src.open("rb") as fin, dst.open("wb+") as fout:
        # 簇 0 留给头部，先写占位，元数据偏移确定后回填
        fout.write(_HEADER.pack(
            QCOW2_MAGIC, 3, 0, 0, CLUSTER_BITS, virtual_size, 0,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 4, _HEADER.size,
        ))
        fout.write(b"\x00" * (CLUSTER_SIZE - _HEADER.size))
        next_offset = CLUSTER_SIZE

        for index in range(clusters):
            chunk = fin.read(CLUSTER_SIZE)
            if chunk is None or not chunk.strip(b"\x00"):
                continue
            if len(chunk) < CLUSTER_SIZE:
                chunk = chunk + b"\x00" * (CLUSTER_SIZE - len(chunk))
            l1_index, l2_index = divmod(index, L2_ENTRIES)
            table_offset = l2_offsets[l1_index]
            if table_offset is None:
                table_offset = next_offset
                l2_offsets[l1_index] = table_offset
                fout.seek(table_offset)
                fout.write(_ZERO_BLOCK)
                next_offset += CLUSTER_SIZE
            fout.seek(next_offset)
            fout.write(chunk)
            fout.seek(table_offset + l2_index * 8)
            fout.write(struct.pack(">Q", next_offset | _COPIED))
            next_offset += CLUSTER_SIZE

        # L1 表：记录每个 L2 表的位置，空槽表示对应区段全为零；
        # refcount 已内联记录，故 L1 项与 L2 数据项都带 COPIED 标志
        l1_offset = next_offset
        fout.seek(l1_offset)
        fout.write(b"".join(
            struct.pack(">Q", (offset | _COPIED) if offset else 0)
            for offset in l2_offsets
        ))
        fout.write(b"\x00" * (_clusters_for(l1_size * 8) * CLUSTER_SIZE - l1_size * 8))
        next_offset += _clusters_for(l1_size * 8) * CLUSTER_SIZE

        used = next_offset // CLUSTER_SIZE
        table_clusters, block_clusters = _refcount_layout(used)
        table_offset = next_offset
        block_offset = table_offset + table_clusters * CLUSTER_SIZE
        total = used + table_clusters + block_clusters

        fout.seek(block_offset)
        for block in range(block_clusters):
            first = block * REFCOUNT_PER_BLOCK
            count = min(REFCOUNT_PER_BLOCK, total - first)
            fout.write(struct.pack(f">{count}H", *([1] * count)))
            fout.write(b"\x00" * ((REFCOUNT_PER_BLOCK - count) * 2))
        fout.seek(table_offset)
        entries = [
            block_offset + block * CLUSTER_SIZE for block in range(block_clusters)
        ]
        entries += [0] * (table_clusters * (CLUSTER_SIZE // 8) - len(entries))
        fout.write(struct.pack(f">{len(entries)}Q", *entries))

        fout.seek(0)
        fout.write(_HEADER.pack(
            QCOW2_MAGIC, 3, 0, 0, CLUSTER_BITS, virtual_size, 0,
            l1_size, l1_offset, table_offset, table_clusters, 0, 0,
            0, 0, 0, 4, _HEADER.size,
        ))


def _clusters_for(length: int) -> int:
    return (length + CLUSTER_SIZE - 1) // CLUSTER_SIZE


def _refcount_layout(used: int) -> tuple[int, int]:
    """求 refcount 表簇数与 refcount 块数的自洽解（元数据自身也要计数）。"""
    table, blocks = 0, 0
    total = used
    while True:
        blocks = _clusters_for(total * 2)
        table = _clusters_for(blocks * 8)
        resolved = used + table + blocks
        if resolved == total:
            return table, blocks
        total = resolved
