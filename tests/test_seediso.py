"""cidata.iso 生成测试：卷标、文件名、内容与临时目录清理。"""

from __future__ import annotations

import pycdlib

from kvm_cloud_init.seediso import temporary_seed, build_seed_iso

DOCS = {
    "meta-data": "instance-id: t\nlocal-hostname: t\n",
    "network-config": "version: 2\n",
    "user-data": "#cloud-config\nhostname: t\n",
}


def _rr_names(iso: pycdlib.PyCdlib) -> set[str]:
    names: set[str] = set()
    for child in iso.list_children(iso_path="/"):
        if child.is_file() and child.rock_ridge is not None:
            names.add(child.rock_ridge.name().decode())
    return names


def test_build_seed_iso(tmp_path) -> None:
    iso_path = build_seed_iso(DOCS, tmp_path / "cidata.iso")

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        assert iso.pvd.volume_identifier.decode().strip() == "cidata"
        assert _rr_names(iso) == {"meta-data", "network-config", "user-data"}

        out = tmp_path / "ud.txt"
        iso.get_file_from_iso(str(out), rr_path="/user-data")
        assert out.read_text() == DOCS["user-data"]

        # Joliet 长名可访问
        iso.get_file_from_iso(str(tmp_path / "j.txt"), joliet_path="/network-config")
        assert (tmp_path / "j.txt").read_text() == DOCS["network-config"]
    finally:
        iso.close()


def test_temporary_seed_cleanup() -> None:
    with temporary_seed(DOCS) as iso_path:
        assert iso_path.exists()
        assert iso_path.name == "cidata.iso"
        parent = iso_path.parent
    # 退出上下文后临时目录被删除
    assert not parent.exists()
