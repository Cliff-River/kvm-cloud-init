"""内置镜像预设与 images.conf 渲染的纯数据测试。"""

from __future__ import annotations

import re

import pytest

from kvm_cloud_init import catalog

REQUIRED_IDS = {
    "almalinux-8",
    "almalinux-9",
    "almalinux-10",
    "rocky-8",
    "rocky-8-lvm",
    "rocky-9",
    "rocky-9-lvm",
    "rocky-10",
    "rocky-10-lvm",
    "centos-stream-9",
    "centos-stream-10",
    "debian-11",
    "debian-12",
    "debian-13",
    "ubuntu-20.04",
    "ubuntu-22.04",
    "ubuntu-24.04",
    "ubuntu-26.04",
    "fedora-43",
    "fedora-44",
}


def test_preset_covers_required_ids() -> None:
    ids = set(catalog.preset_ids())
    assert REQUIRED_IDS <= ids


def test_preset_ids_and_files_are_unique() -> None:
    ids = [entry.id for entry in catalog.PRESET]
    files = [entry.file for entry in catalog.PRESET]
    assert len(ids) == len(set(ids))
    assert len(files) == len(set(files))


@pytest.mark.parametrize("entry", catalog.PRESET, ids=lambda e: e.id)
def test_preset_schema_is_consistent(entry: catalog.PresetEntry) -> None:
    assert entry.kind in ("url", "index")
    assert entry.checksum_algo in ("sha256", "sha512")
    assert entry.file.endswith((".qcow2", ".img", ".raw"))
    assert "/" not in entry.file
    if entry.kind == "url":
        assert entry.urls and not entry.pages and not entry.match
        assert entry.checksum_url
    else:
        assert entry.pages and not entry.urls and not entry.checksum_url
        assert entry.match
        re.compile(entry.match)


@pytest.mark.parametrize(
    "entry", [item for item in catalog.PRESET if not item.archived],
    ids=lambda e: e.id,
)
def test_non_archived_match_compiles(entry: catalog.PresetEntry) -> None:
    if entry.match:
        re.compile(entry.match)
    if entry.checksum_match:
        re.compile(entry.checksum_match)


def test_render_is_deterministic() -> None:
    first = catalog.render_catalog()
    second = catalog.render_catalog()
    assert first == second
    assert "请勿手工编辑" in first
    for image_id in REQUIRED_IDS:
        assert f"{image_id}:" in first


def test_render_with_probes_writes_metadata() -> None:
    text = catalog.render_catalog(
        {
            "rocky-10-lvm": {
                "upstream_file": "Rocky-10-GenericCloud-LVM-10.2.x86_64.qcow2",
                "upstream_version": "10.2",
                "checked_at": "2026-10-25T00:00:00Z",
                "upstream_status": "ok",
            }
        }
    )
    assert "upstream_file: Rocky-10-GenericCloud-LVM-10.2.x86_64.qcow2" in text
    assert "upstream_status: ok" in text
    # 未探测的条目不会被写入元数据
    almalinux = text.split("almalinux-8:", 1)[1].split("almalinux-9:", 1)[0]
    assert "upstream_file" not in almalinux
