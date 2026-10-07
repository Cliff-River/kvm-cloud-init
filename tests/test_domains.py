"""快照残留文件收集与 XML 解析测试（不连接真实 libvirt）。"""

from __future__ import annotations

from kvm_cloud_init.hv import domains

DOMAIN_XML = """\
<domain type='kvm'>
  <name>vm</name>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/pool/vm.ov1'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='/pool/vm-cidata.iso'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
  </devices>
</domain>
"""

SNAPSHOT_XML = """\
<domainsnapshot>
  <name>s1</name>
  <memory snapshot='external' file='/pool/vm.mem'/>
  <disks>
    <disk name='vda' snapshot='external'>
      <source file='/pool/vm.ov1'/>
    </disk>
  </disks>
</domainsnapshot>
"""


class _FakeSnapshot:
    def __init__(self, xml: str) -> None:
        self._xml = xml

    def getXMLDesc(self, flags: int = 0) -> str:
        return self._xml


class _FakeDomain:
    def __init__(self, xml: str, snapshot_xml: str | None) -> None:
        self._xml = xml
        self._snapshot_xml = snapshot_xml

    def XMLDesc(self, flags: int = 0) -> str:
        return self._xml

    def snapshotListNames(self, flags: int = 0) -> list[str]:
        return ["s1"] if self._snapshot_xml else []

    def snapshotLookupByName(self, name: str, flags: int = 0) -> _FakeSnapshot:
        return _FakeSnapshot(self._snapshot_xml or "")


def test_domain_disk_files() -> None:
    assert domains.domain_disk_files(DOMAIN_XML) == {
        "/pool/vm.ov1",
        "/pool/vm-cidata.iso",
    }


def test_snapshot_files() -> None:
    assert domains.snapshot_files(SNAPSHOT_XML) == {
        "/pool/vm.ov1",
        "/pool/vm.mem",
    }


def test_collect_leftovers_with_backing_chain(make_qcow2, tmp_path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    # overlay 指向相对路径的基础镜像；内存文件与 iso 不是 qcow2
    make_qcow2(pool_dir / "vm.base", 1024)
    make_qcow2(pool_dir / "vm.ov1", 2048, backing="vm.base")
    (pool_dir / "vm.mem").write_bytes(b"\x00" * 64)

    # 把 XML 中的 /pool 替换为真实临时目录
    pool_xml = DOMAIN_XML.replace("/pool", str(pool_dir))
    snap_xml = SNAPSHOT_XML.replace("/pool", str(pool_dir))

    leftovers = domains.collect_leftover_files(
        conn=None,
        dom=_FakeDomain(pool_xml, snap_xml),
        pool_path=str(pool_dir),
    )
    assert set(leftovers) == {
        str(pool_dir / "vm.ov1"),
        str(pool_dir / "vm.base"),
        str(pool_dir / "vm.mem"),
        str(pool_dir / "vm-cidata.iso"),
    }


def test_collect_without_snapshots(make_qcow2, tmp_path) -> None:
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    make_qcow2(pool_dir / "vm.qcow2", 1024)
    xml = DOMAIN_XML.replace("/pool", str(pool_dir)).replace("vm.ov1", "vm.qcow2")
    leftovers = domains.collect_leftover_files(
        conn=None, dom=_FakeDomain(xml, None), pool_path=str(pool_dir)
    )
    # 无快照时仅域自身磁盘 + 光驱；qcow2 无 backing，不扩展
    assert leftovers == sorted({
        str(pool_dir / "vm.qcow2"),
        str(pool_dir / "vm-cidata.iso"),
    })


def test_outside_pool_is_filtered() -> None:
    xml = DOMAIN_XML.replace("/pool", "/elsewhere")
    leftovers = domains.collect_leftover_files(
        conn=None, dom=_FakeDomain(xml, None), pool_path="/pool"
    )
    assert leftovers == []
