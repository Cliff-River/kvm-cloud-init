"""存储卷 XML 生成测试。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kvm_cloud_init.hv import storage


def test_volume_xml_qcow2() -> None:
    root = ET.fromstring(storage.volume_xml("vm.qcow2", 12345, "qcow2"))
    assert root.findtext("name") == "vm.qcow2"
    assert root.findtext("capacity") == "12345"
    assert root.find("capacity").get("unit") == "bytes"
    assert root.find("./target/format").get("type") == "qcow2"


def test_volume_xml_raw() -> None:
    root = ET.fromstring(storage.volume_xml("vm-cidata.iso", 4096, "raw"))
    assert root.find("./target/format").get("type") == "raw"
