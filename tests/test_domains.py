"""关机、undefine、快照残留收集与 XML 解析测试（不连接真实 libvirt）。"""

from __future__ import annotations

import libvirt
import pytest

from kvm_cloud_init.errors import ShutdownTimeout
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
        _FakeDomain(pool_xml, snap_xml), str(pool_dir)
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
    leftovers = domains.collect_leftover_files(_FakeDomain(xml, None), str(pool_dir))
    # 无快照时仅域自身磁盘 + 光驱；qcow2 无 backing，不扩展
    assert leftovers == sorted({
        str(pool_dir / "vm.qcow2"),
        str(pool_dir / "vm-cidata.iso"),
    })


def test_outside_pool_is_filtered() -> None:
    xml = DOMAIN_XML.replace("/pool", "/elsewhere")
    leftovers = domains.collect_leftover_files(_FakeDomain(xml, None), "/pool")
    assert leftovers == []


# ---- shutdown_and_wait -----------------------------------------------------

class _FakeShutdownDomain:
    """按预置序列返回状态，记录 shutdown/destroy 调用。"""

    def __init__(self, states: list[int]) -> None:
        self._states = states
        self.shutdown_calls = 0
        self.destroy_calls = 0

    def name(self) -> str:
        return "vm"

    def state(self, flags: int = 0):
        return [self._states.pop(0), 1]

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def destroy(self) -> None:
        self.destroy_calls += 1


def _patch_clock(monkeypatch, readings: list[float]) -> None:
    """让 time.monotonic 按给定读数推进，并消除真实 sleep。"""
    ticks = iter(readings)
    monkeypatch.setattr(domains.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(domains.time, "sleep", lambda _seconds: None)


def test_shutdown_already_off_logs_and_skips_signal() -> None:
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_SHUTOFF])
    logs: list[str] = []
    domains.shutdown_and_wait(dom, timeout=5, log=logs.append)
    assert dom.shutdown_calls == 0
    assert any("already shut off" in line for line in logs)


def test_shutdown_running_sends_signal_and_waits(monkeypatch) -> None:
    _patch_clock(monkeypatch, [100.0, 100.0])
    dom = _FakeShutdownDomain(
        [libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_SHUTOFF]
    )
    logs: list[str] = []
    domains.shutdown_and_wait(dom, timeout=5, log=logs.append)
    assert dom.shutdown_calls == 1
    assert dom.destroy_calls == 0
    assert any("sending shutdown signal" in line for line in logs)
    assert any("has shut down" in line for line in logs)


def test_shutdown_paused_does_not_send_signal(monkeypatch) -> None:
    # PAUSED 等非运行态只轮询，不应调用 shutdown()
    _patch_clock(monkeypatch, [100.0, 100.0])
    dom = _FakeShutdownDomain(
        [libvirt.VIR_DOMAIN_PAUSED, libvirt.VIR_DOMAIN_SHUTOFF]
    )
    logs: list[str] = []
    domains.shutdown_and_wait(dom, timeout=5, log=logs.append)
    assert dom.shutdown_calls == 0
    assert any("has shut down" in line for line in logs)


def test_shutdown_reports_progress_every_five_seconds(monkeypatch) -> None:
    # 每轮两次 monotonic：while 条件 + elapsed 计算。
    # deadline=110；轮询 105（elapsed=5，播报）-> 条件 110（退出循环）
    _patch_clock(monkeypatch, [100.0, 105.0, 105.0, 110.0])
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_RUNNING])
    logs: list[str] = []
    domains.shutdown_and_wait(dom, timeout=10, force=True, log=logs.append)
    assert dom.destroy_calls == 1
    assert any("5s elapsed" in line for line in logs)
    assert any("forcing power off" in line for line in logs)


class _FakeTty:
    """记录写入内容的假 TTY stdout。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> None:
        self.chunks.append(text)

    def flush(self) -> None:
        pass


def test_shutdown_progress_inline_on_tty(monkeypatch) -> None:
    _patch_clock(monkeypatch, [100.0, 105.0, 105.0, 110.0])
    tty = _FakeTty()
    monkeypatch.setattr(domains.sys, "stdout", tty)
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_RUNNING])

    domains.shutdown_and_wait(dom, timeout=10, force=True)

    written = "".join(tty.chunks)
    assert written.count("Waiting for instance vm to shut down") == 1
    assert "\r\x1b[2K" in written
    assert dom.destroy_calls == 1


def test_shutdown_timeout_without_force_raises(monkeypatch) -> None:
    _patch_clock(monkeypatch, [100.0, 105.0, 105.0, 110.0])
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_RUNNING])
    with pytest.raises(ShutdownTimeout, match="did not shut down within 10s"):
        domains.shutdown_and_wait(dom, timeout=10, force=False, log=lambda _line: None)
    assert dom.destroy_calls == 0


def test_shutdown_resends_signal_then_forces_off(monkeypatch) -> None:
    # 初次信号 + 两次重发（每次间隔 10s）后，第 3 次信号用尽即强制断电
    _patch_clock(monkeypatch, [0.0, 10.0, 20.0, 30.0])
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_RUNNING] * 4)
    logs: list[str] = []
    domains.shutdown_and_wait(
        dom,
        timeout=100,
        force=True,
        log=logs.append,
        retry_interval=10,
        max_attempts=3,
    )
    assert dom.shutdown_calls == 3
    assert dom.destroy_calls == 1
    assert any("resending shutdown signal (attempt 2 of 3)" in line for line in logs)
    assert any("resending shutdown signal (attempt 3 of 3)" in line for line in logs)
    assert any("forcing power off" in line for line in logs)


def test_shutdown_stops_resending_without_force_and_times_out(monkeypatch) -> None:
    # force=False 时信号次数用尽后不再重发，等满 timeout 再抛错
    _patch_clock(monkeypatch, [0.0, 10.0, 20.0, 25.0])
    dom = _FakeShutdownDomain([libvirt.VIR_DOMAIN_RUNNING] * 4)
    with pytest.raises(ShutdownTimeout, match="did not shut down within 25s"):
        domains.shutdown_and_wait(
            dom,
            timeout=25,
            force=False,
            log=lambda _line: None,
            retry_interval=10,
            max_attempts=2,
        )
    assert dom.shutdown_calls == 2
    assert dom.destroy_calls == 0


# ---- destroy_domain --------------------------------------------------------

class _FakePool:
    def __init__(self, path: str) -> None:
        self._path = path
        self.refreshes = 0

    def XMLDesc(self, flags: int = 0) -> str:
        return f"<pool><target><path>{self._path}</path></target></pool>"

    def info(self):
        return [libvirt.VIR_STORAGE_POOL_RUNNING]

    def create(self, flags: int = 0) -> None:
        pass

    def refresh(self, flags: int = 0) -> None:
        self.refreshes += 1


class _FakeDestroyConn:
    def __init__(self, dom, pool) -> None:
        self._dom = dom
        self._pool = pool

    def storagePoolLookupByName(self, name: str):
        return self._pool

    def lookupByName(self, name: str):
        if self._dom is None:
            raise libvirt.libvirtError(f"domain {name} not found")
        return self._dom


class _FakeDestroyableDomain(_FakeDomain):
    def __init__(self, xml: str) -> None:
        super().__init__(xml, None)
        self.undefine_flags: int | None = None

    def name(self) -> str:
        return "vm"

    def state(self, flags: int = 0):
        return [libvirt.VIR_DOMAIN_SHUTOFF, 1]

    def undefineFlags(self, flags: int) -> None:
        self.undefine_flags = flags


def test_destroy_domain_missing_returns_false() -> None:
    conn = _FakeDestroyConn(None, _FakePool("/pool"))
    assert domains.destroy_domain(conn, "vm", "default", 5) is False


def test_destroy_domain_full_flow(monkeypatch) -> None:
    dom = _FakeDestroyableDomain(DOMAIN_XML)
    pool = _FakePool("/pool")
    conn = _FakeDestroyConn(dom, pool)

    deleted: list[str] = []
    monkeypatch.setattr(
        domains.storage, "delete_path", lambda _conn, _pool, path: deleted.append(path)
    )

    logs: list[str] = []
    existed = domains.destroy_domain(conn, "vm", "default", 5, log=logs.append)

    assert existed is True
    assert dom.undefine_flags == domains._UNDEFINE_FLAGS
    assert pool.refreshes == 2
    assert set(deleted) == {"/pool/vm.ov1", "/pool/vm-cidata.iso"}
    assert any("Undefining domain" in line for line in logs)
    assert any("Cleaning up 2 leftover storage file(s)" in line for line in logs)


# ---- stop_domain / 共用关机逻辑 --------------------------------------------


def test_stop_domain_missing_returns_false() -> None:
    conn = _FakeDestroyConn(None, _FakePool("/pool"))
    assert domains.stop_domain(conn, "vm", 5) is False


def test_stop_domain_delegates_to_shutdown_and_wait(monkeypatch) -> None:
    dom = object()
    conn = _FakeDestroyConn(dom, _FakePool("/pool"))
    calls: list[tuple] = []

    def fake_shutdown(target, timeout, force=False, log=print) -> None:
        calls.append((target, timeout, force))

    monkeypatch.setattr(domains, "shutdown_and_wait", fake_shutdown)

    assert domains.stop_domain(conn, "vm", 7, force=True) is True
    assert calls == [(dom, 7, True)]


def test_destroy_domain_uses_shared_shutdown(monkeypatch) -> None:
    dom = _FakeDestroyableDomain(DOMAIN_XML)
    conn = _FakeDestroyConn(dom, _FakePool("/pool"))
    calls: list[tuple] = []

    def fake_shutdown(target, timeout, force=False, log=print) -> None:
        calls.append((target, timeout, force))

    monkeypatch.setattr(domains, "shutdown_and_wait", fake_shutdown)
    monkeypatch.setattr(domains.storage, "delete_path", lambda *args: None)

    assert domains.destroy_domain(conn, "vm", "default", 9, force=True) is True
    assert calls == [(dom, 9, True)]
