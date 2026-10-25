from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kvm_cloud_init import imagefmt, qcow2
from kvm_cloud_init.errors import ImageFetchError

_CLUSTER = imagefmt.CLUSTER_SIZE
_OFFSET_MASK = 0x00FFFFFFFFFFFE00
_HEADER_KEYS = [
    "magic", "version", "backing_offset", "backing_size", "cluster_bits",
    "size", "crypt", "l1_size", "l1_offset", "refcount_offset",
    "refcount_clusters", "nb_snapshots", "snapshots_offset", "incompatible",
    "compatible", "autoclear", "refcount_order", "header_length",
]


def _header(path: Path) -> dict[str, int]:
    raw = path.read_bytes()[:104]
    return dict(zip(_HEADER_KEYS, struct.unpack(">4sIQIIQIIQQIIQQQQII", raw)))


def _l1(path: Path, info: dict[str, int]) -> list[int]:
    raw = path.read_bytes()
    start = info["l1_offset"]
    return list(struct.unpack(
        f">{info['l1_size']}Q", raw[start : start + info["l1_size"] * 8]
    ))


def _l2_entry(path: Path, table: int, index: int) -> int:
    raw = path.read_bytes()
    offset = (table & _OFFSET_MASK) + index * 8
    return struct.unpack(">Q", raw[offset : offset + 8])[0]


def _cluster_at(path: Path, value: int) -> bytes:
    raw = path.read_bytes()
    offset = value & _OFFSET_MASK
    return raw[offset : offset + _CLUSTER]


def _raw(path: Path, clusters: list[bytes]) -> Path:
    path.write_bytes(b"".join(clusters))
    return path


def test_detect_recognises_qcow2_and_raw(tmp_path: Path) -> None:
    raw = tmp_path / "a.raw"
    raw.write_bytes(b"\x00" * 16)
    assert imagefmt.detect(raw) == "raw"

    qcow = tmp_path / "a.qcow2"
    qcow.write_bytes(qcow2.QCOW2_MAGIC + b"\x00" * 12)
    assert imagefmt.detect(qcow) == "qcow2"


def test_convert_preserves_data_and_marks_zero_clusters(tmp_path: Path) -> None:
    src = _raw(tmp_path / "disk.raw", [
        b"A" * _CLUSTER,
        b"\x00" * _CLUSTER,
        b"B" * 1024 + b"\x00" * (_CLUSTER - 1024),
    ])
    dst = tmp_path / "disk.qcow2"
    imagefmt.convert(src, dst, "qcow2")

    info = _header(dst)
    assert info["magic"] == qcow2.QCOW2_MAGIC
    assert info["version"] == 3
    assert info["size"] == 3 * _CLUSTER
    assert info["cluster_bits"] == imagefmt.CLUSTER_BITS
    assert info["refcount_order"] == 4
    assert qcow2.read_info(str(dst)).virtual_size == 3 * _CLUSTER

    l1 = _l1(dst, info)
    assert len(l1) == 1 and l1[0] != 0
    assert _cluster_at(dst, _l2_entry(dst, l1[0], 0)) == b"A" * _CLUSTER
    assert _l2_entry(dst, l1[0], 1) == 0  # 全零簇不分配
    assert _cluster_at(dst, _l2_entry(dst, l1[0], 2)) == src.read_bytes()[2 * _CLUSTER :]


def test_convert_writes_consistent_refcounts(tmp_path: Path) -> None:
    src = _raw(tmp_path / "disk.raw", [b"Q" * _CLUSTER, b"\x00" * _CLUSTER])
    dst = tmp_path / "disk.qcow2"
    imagefmt.convert(src, dst, "qcow2")

    info = _header(dst)
    raw = dst.read_bytes()
    total = len(raw) // _CLUSTER
    entries = struct.unpack(
        ">Q", raw[info["refcount_offset"] : info["refcount_offset"] + 8]
    )[0]
    counts = struct.unpack(f">{_CLUSTER // 2}H", raw[entries : entries + _CLUSTER])
    assert counts[:total] == (1,) * total
    assert counts[total:] == (0,) * (len(counts) - total)
    # 所有元数据偏移必须簇对齐
    assert info["l1_offset"] % _CLUSTER == 0
    assert info["refcount_offset"] % _CLUSTER == 0
    assert entries % _CLUSTER == 0


def test_convert_rounds_virtual_size_up_to_sector(tmp_path: Path) -> None:
    src = tmp_path / "disk.raw"
    src.write_bytes(b"hello")
    dst = tmp_path / "disk.qcow2"
    imagefmt.convert(src, dst, "qcow2")
    assert qcow2.read_info(str(dst)).virtual_size == 512


def test_convert_all_zero_raw_allocates_no_data(tmp_path: Path) -> None:
    src = _raw(tmp_path / "disk.raw", [b"\x00" * _CLUSTER] * 2)
    dst = tmp_path / "disk.qcow2"
    imagefmt.convert(src, dst, "qcow2")
    info = _header(dst)
    assert _l1(dst, info) == [0]
    # 头部 + L1 + refcount 表 + refcount 块，不含数据簇
    assert len(dst.read_bytes()) == 4 * _CLUSTER


def test_convert_rejects_same_format_and_unknown(tmp_path: Path) -> None:
    src = tmp_path / "disk.raw"
    src.write_bytes(b"x" * 512)
    with pytest.raises(ImageFetchError, match="already raw"):
        imagefmt.convert(src, tmp_path / "o.raw", "raw")
    with pytest.raises(ImageFetchError, match="Unsupported image format"):
        imagefmt.convert(src, tmp_path / "o.img", "vmdk")

    qcow = tmp_path / "a.qcow2"
    qcow.write_bytes(qcow2.QCOW2_MAGIC + b"\x00" * 100)
    with pytest.raises(ImageFetchError, match="not supported"):
        imagefmt.convert(qcow, tmp_path / "o.raw", "raw")


def test_convert_rejects_empty_source(tmp_path: Path) -> None:
    src = tmp_path / "disk.raw"
    src.write_bytes(b"")
    with pytest.raises(ImageFetchError, match="empty"):
        imagefmt.convert(src, tmp_path / "o.qcow2", "qcow2")
