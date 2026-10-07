"""qcow2 头解析、容量换算与 backing chain 测试。"""

from __future__ import annotations

import pytest

from kvm_cloud_init import qcow2


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("100G", 100 * 1024**3),
            ("512m", 512 * 1024**2),
            ("2T", 2 * 1024**4),
            ("1024", 1024),
            ("1.5G", int(1.5 * 1024**3)),
            ("100", 100),
        ],
    )
    def test_values(self, text: str, expected: int) -> None:
        assert qcow2.parse_size(text) == expected

    @pytest.mark.parametrize("bad", ["", "abc", "-5G", "0", "5X", "0G"])
    def test_bad(self, bad: str) -> None:
        with pytest.raises(ValueError):
            qcow2.parse_size(bad)

    def test_int_passthrough(self) -> None:
        assert qcow2.parse_size(2048) == 2048
        with pytest.raises(ValueError):
            qcow2.parse_size(0)


class TestQcow2Header:
    def test_read_info(self, make_qcow2, tmp_path) -> None:
        path = make_qcow2(tmp_path / "a.qcow2", 64 * 1024**2)
        info = qcow2.read_info(str(path))
        assert info.virtual_size == 64 * 1024**2
        assert info.backing_file is None

    def test_backing_relative(self, make_qcow2, tmp_path) -> None:
        base = make_qcow2(tmp_path / "base.qcow2", 1024)
        overlay = make_qcow2(tmp_path / "ov1.qcow2", 2048, backing="base.qcow2")
        chain = list(qcow2.iter_backing_chain(str(overlay)))
        assert chain == [str(overlay.resolve()), str(base.resolve())]

    def test_bad_magic_raises(self, tmp_path) -> None:
        bad = tmp_path / "bad.qcow2"
        bad.write_bytes(b"not a qcow2 file" + b"\0" * 100)
        with pytest.raises(ValueError, match="不是有效的 qcow2"):
            qcow2.read_info(str(bad))

    def test_chain_stops_without_backing(self, make_qcow2, tmp_path) -> None:
        single = make_qcow2(tmp_path / "s.qcow2", 1)
        assert list(qcow2.iter_backing_chain(str(single))) == [str(single.resolve())]
