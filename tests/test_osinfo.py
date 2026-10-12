"""osinfo OS ID 解析测试。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from kvm_cloud_init.hv import osinfo


def _os_xml(short_id: str, os_id: str) -> bytes:
    return f"""<libosinfo version="0.0.1">
  <os id="{os_id}">
    <short-id>{short_id}</short-id>
    <name>Test OS</name>
  </os>
</libosinfo>""".encode()


def test_resolve_from_local_db(tmp_path: Path, monkeypatch) -> None:
    vendor = tmp_path / "rockylinux.org"
    vendor.mkdir(parents=True)
    (vendor / "rocky-10.xml").write_bytes(_os_xml("rocky10", "http://rockylinux.org/rocky/10"))
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", (tmp_path,))
    osinfo.reset_cache()

    assert osinfo.resolve_os_id("rocky10") == "http://rockylinux.org/rocky/10"
    osinfo.reset_cache()


def test_resolve_online_fallback(monkeypatch) -> None:
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", ())
    monkeypatch.setattr(osinfo, "_BUILTIN_IDS", {})
    osinfo.reset_cache()

    fake_resp = MagicMock()
    fake_resp.read.return_value = _os_xml("rocky10", "http://rockylinux.org/rocky/10")
    fake_resp.__enter__.return_value = fake_resp
    with patch("kvm_cloud_init.hv.osinfo.urllib.request.urlopen", return_value=fake_resp) as m:
        assert osinfo.resolve_os_id("rocky10") == "http://rockylinux.org/rocky/10"
        assert m.call_count == 1
    osinfo.reset_cache()


def test_resolve_builtin_fallback(monkeypatch) -> None:
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", ())
    monkeypatch.setattr(osinfo, "_BUILTIN_IDS", {"rocky10": "http://rockylinux.org/rocky/10"})
    osinfo.reset_cache()

    # 网络失败时回退到内置映射
    with patch("kvm_cloud_init.hv.osinfo.urllib.request.urlopen", side_effect=OSError):
        assert osinfo.resolve_os_id("rocky10") == "http://rockylinux.org/rocky/10"
    osinfo.reset_cache()


def test_resolve_derived_fallback(monkeypatch) -> None:
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", ())
    monkeypatch.setattr(osinfo, "_BUILTIN_IDS", {})
    osinfo.reset_cache()

    with patch("kvm_cloud_init.hv.osinfo.urllib.request.urlopen", side_effect=OSError):
        assert osinfo.resolve_os_id("centos9") == "http://centos.org/centos/9"
    osinfo.reset_cache()


def test_resolve_unknown_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", ())
    monkeypatch.setattr(osinfo, "_BUILTIN_IDS", {})
    osinfo.reset_cache()

    with patch("kvm_cloud_init.hv.osinfo.urllib.request.urlopen", side_effect=OSError):
        assert osinfo.resolve_os_id("totally-unknown-99") is None
    osinfo.reset_cache()


def test_local_db_invalid_xml_skipped(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "bad.org"
    bad.mkdir(parents=True)
    (bad / "broken.xml").write_bytes(b"<not valid xml")
    good = tmp_path / "good.org"
    good.mkdir(parents=True)
    (good / "ok.xml").write_bytes(_os_xml("debian13", "http://debian.org/debian/13"))
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", (tmp_path,))
    osinfo.reset_cache()

    assert osinfo.resolve_os_id("debian13") == "http://debian.org/debian/13"
    osinfo.reset_cache()


def test_parse_os_xml_direct_os_root() -> None:
    data = b'<os id="http://example.com/x/1"><short-id>x1</short-id></os>'
    assert osinfo._parse_os_xml(data) == ("x1", "http://example.com/x/1")


def test_cache_avoids_repeated_network(monkeypatch) -> None:
    monkeypatch.setattr(osinfo, "_OSINFO_OS_PATHS", ())
    monkeypatch.setattr(osinfo, "_BUILTIN_IDS", {})
    osinfo.reset_cache()

    fake_resp = MagicMock()
    fake_resp.read.return_value = _os_xml("rocky10", "http://rockylinux.org/rocky/10")
    fake_resp.__enter__.return_value = fake_resp
    with patch("kvm_cloud_init.hv.osinfo.urllib.request.urlopen", return_value=fake_resp) as m:
        osinfo.resolve_os_id("rocky10")
        osinfo.resolve_os_id("rocky10")
        assert m.call_count == 1  # 第二次走缓存
    osinfo.reset_cache()
