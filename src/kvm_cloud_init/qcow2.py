"""qcow2 镜像头解析与容量单位换算（标准库实现，无需 qemu-img）。

仅读取 qcow2 头部中本项目关心的固定字段：
- 虚拟大小（virtual size，偏移 24，大端 uint64）
- backing file 路径（偏移由头部 8/16 处的 offset/size 指定）

格式参考：https://gitlab.com/qemu-project/qemu/-/blob/master/docs/interop/qcow2.txt
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass

QCOW2_MAGIC = b"QFI\xfb"
_VIRTUAL_SIZE_OFFSET = 24
_BACKING_OFFSET_OFFSET = 8
_BACKING_SIZE_OFFSET = 16
_HEADER_SIZE = 104

# qemu-img 风格后缀，按二进制（1024）换算
_UNIT_FACTORS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
    "E": 1024**6,
}


@dataclass(frozen=True)
class Qcow2Info:
    """qcow2 头部信息。"""

    virtual_size: int
    backing_file: str | None


def parse_size(value: str | int) -> int:
    """把 ``100G`` / ``512M`` / ``1024`` 这类容量字符串解析为字节数。

    规则与 qemu-img 一致：K/M/G/T/P/E 均按 1024 进位，大小写不敏感，
    无后缀时按字节处理。
    """
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"容量必须为正数：{value}")
        return value
    text = str(value).strip().upper()
    if not text:
        raise ValueError("容量不能为空")
    suffix = text[-1] if not text[-1].isdigit() else ""
    number = text[: -len(suffix)] if suffix else text
    if suffix not in _UNIT_FACTORS:
        raise ValueError(f"无法识别的容量单位：{value!r}（支持 K/M/G/T/P/E）")
    try:
        amount = float(number) if "." in number else int(number)
    except ValueError as exc:
        raise ValueError(f"无法解析容量：{value!r}") from exc
    if amount <= 0:
        raise ValueError(f"容量必须为正数：{value!r}")
    return int(amount * _UNIT_FACTORS[suffix])


def read_info(path: str) -> Qcow2Info:
    """读取 qcow2 文件头，返回虚拟大小与 backing file（若有）。"""
    with open(path, "rb") as fh:
        header = fh.read(_HEADER_SIZE)
    if len(header) < _HEADER_SIZE or header[:4] != QCOW2_MAGIC:
        raise ValueError(f"{path} 不是有效的 qcow2 文件")
    backing_offset = struct.unpack(">Q", header[_BACKING_OFFSET_OFFSET : _BACKING_OFFSET_OFFSET + 8])[0]
    backing_size = struct.unpack(">I", header[_BACKING_SIZE_OFFSET : _BACKING_SIZE_OFFSET + 4])[0]
    virtual_size = struct.unpack(">Q", header[_VIRTUAL_SIZE_OFFSET : _VIRTUAL_SIZE_OFFSET + 8])[0]
    backing_name: str | None = None
    if backing_offset and backing_size:
        with open(path, "rb") as fh:
            fh.seek(backing_offset)
            raw = fh.read(backing_size)
        backing_name = raw.rstrip(b"\x00").decode("utf-8", errors="replace") or None
    return Qcow2Info(virtual_size=virtual_size, backing_file=backing_name)


def iter_backing_chain(path: str) -> Iterator[str]:
    """沿 backing file 链向上遍历，依次产出链上每一级文件的绝对路径。

    相对路径按其直接上层 overlay 所在目录解析（与 qemu 行为一致）。
    链中包含入参文件自身；不存在或无法解析的节点在其处终止。
    """
    current = os.path.abspath(path)
    visited: set[str] = set()
    while current and current not in visited and os.path.isfile(current):
        visited.add(current)
        yield current
        parent = read_info(current).backing_file
        if not parent:
            break
        if not os.path.isabs(parent):
            parent = os.path.join(os.path.dirname(current), parent)
        current = os.path.abspath(parent)
