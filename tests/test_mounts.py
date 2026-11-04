"""``mount`` / ``unmount`` 编排测试（fake libvirt 域 + 脚本化 SSH 输出）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import libvirt
import pytest

from kvm_cloud_init.errors import MountError
from kvm_cloud_init.hv import filesystem as fsmod
from kvm_cloud_init.hv.guest import METADATA_NS, build_domain_xml
from kvm_cloud_init.provision import mounts


def _base_xml() -> str:
    with patch(
        "kvm_cloud_init.hv.guest.resolve_os_id",
        return_value="http://rockylinux.org/rocky/10",
    ):
        return build_domain_xml(
            name="vm1",
            disk_path="/pool/vm1.qcow2",
            iso_path="/pool/vm1-cidata.iso",
            memory_mib=1024,
            vcpus=1,
            network="default",
            firmware="bios",
            os_variant="rocky10",
            template_name="Rocky-LVM",
            ssh_user="cliff",
        )


class FakeDom:
    def __init__(self, xml: str, state: int = libvirt.VIR_DOMAIN_RUNNING) -> None:
        self._xml = xml
        self._state = state
        self.attached: list[str] = []
        self.detached: list[str] = []

    def name(self) -> str:
        return "vm1"

    def state(self, _flags: int):
        return [self._state, 1]

    def XMLDesc(self, flags: int) -> str:
        return self._xml

    def attachDeviceFlags(self, xml: str, _flags: int) -> None:
        self.attached.append(xml)

    def detachDeviceFlags(self, xml: str, _flags: int) -> None:
        self.detached.append(xml)


class FakeConn:
    def __init__(self, dom: FakeDom) -> None:
        self.dom = dom

    def defineXML(self, xml: str) -> FakeDom:
        self.dom._xml = xml
        return self.dom


@pytest.fixture
def env(monkeypatch):
    dom = FakeDom(_base_xml())
    conn = FakeConn(dom)
    monkeypatch.setattr(mounts.domains, "lookup_domain", lambda _c, _n: dom)
    monkeypatch.setattr(mounts.access, "_ensure_running", lambda *a: None)
    monkeypatch.setattr(
        mounts.access,
        "wait_ssh_target",
        lambda _conn, _name, *, deadline=None: (
            "10.0.0.5", "cliff", Path("/keys/vm1")
        ),
    )
    monkeypatch.setattr(
        mounts.access,
        "ssh_target",
        lambda _conn, _name: ("10.0.0.5", "cliff", Path("/keys/vm1")),
    )
    return conn, dom


def _script_capture(monkeypatch, scripted: list[tuple[str, str, int]]):
    """按脚本应答 capture_command，记录完整 shell 命令。"""
    remaining = list(scripted)
    commands: list[str] = []

    def fake_capture(instance, host, user, key_path, command, *, timeout=None, deadline=None):
        commands.append(command)
        return remaining.pop(0)

    monkeypatch.setattr(mounts.ssh, "capture_command", fake_capture)
    return commands


# ---- mount ----


def test_mount_as_root_runs_mkdir_and_mount_then_attaches(
    env, tmp_path: Path, monkeypatch, capsys
) -> None:
    conn, dom = env
    share = tmp_path / "share"
    share.mkdir()
    commands = _script_capture(
        monkeypatch,
        [
            ("0\n", "", 0),       # id -u
            ("", "", 0),          # mkdir -p
            ("", "", 0),          # mount
        ],
    )

    rc = mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")

    assert rc == 0
    assert commands == [
        "id -u",
        "mkdir -p /mnt/share",
        (
            f"mount -t 9p -o {mounts._MOUNT_OPTIONS} "
            f"{fsmod.mount_tag('vm1', '/mnt/share')} /mnt/share"
        ),
    ]
    assert len(dom.attached) == 1
    root = ET.fromstring(dom.XMLDesc(0))
    mount = root.find(f".//{{{METADATA_NS}}}mount[@target='/mnt/share']")
    assert mount is not None
    assert mount.get("source") == str(share)
    assert "Mounted" in capsys.readouterr().out


def test_mount_as_non_root_uses_passwordless_sudo(
    env, tmp_path: Path, monkeypatch
) -> None:
    conn, _dom = env
    share = tmp_path / "share"
    share.mkdir()
    commands = _script_capture(
        monkeypatch,
        [("1000\n", "", 0), ("", "", 0), ("", "", 0)],
    )

    mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")

    assert commands[1] == "sudo -n mkdir -p /mnt/share"
    assert commands[2].startswith("sudo -n mount -t 9p")


def test_mount_failure_rolls_back_device(
    env, tmp_path: Path, monkeypatch
) -> None:
    conn, dom = env
    share = tmp_path / "share"
    share.mkdir()
    _script_capture(
        monkeypatch,
        [
            ("0\n", "", 0),
            ("", "", 0),
            ("", "mount: unknown filesystem type '9p'", 32),
        ],
    )

    with pytest.raises(MountError, match="Cannot mount"):
        mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")

    # 回滚：实时 detach + 持久配置移除
    assert len(dom.attached) == 1
    assert len(dom.detached) == 1
    assert fsmod.list_mounts(dom.XMLDesc(0)) == []
    assert ET.fromstring(dom.XMLDesc(0)).find("./devices/filesystem") is None


def test_mount_requires_existing_local_dir(env, tmp_path: Path) -> None:
    conn, dom = env
    with pytest.raises(MountError, match="Local directory does not exist"):
        mounts.mount_instance(
            object(), conn, "vm1", str(tmp_path / "missing"), "/mnt/share"
        )
    assert dom.attached == []


def test_mount_requires_absolute_guest_path(env, tmp_path: Path) -> None:
    conn, _dom = env
    share = tmp_path / "share"
    share.mkdir()
    with pytest.raises(MountError, match="absolute path"):
        mounts.mount_instance(object(), conn, "vm1", str(share), "mnt/share")


def test_mount_rejects_unknown_instance(monkeypatch, tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(mounts.domains, "lookup_domain", lambda _c, _n: None)
    with pytest.raises(MountError, match="does not exist"):
        mounts.mount_instance(
            object(), object(), "ghost", str(share), "/mnt/share"
        )


def test_mount_rejects_duplicate_target(
    env, tmp_path: Path, monkeypatch
) -> None:
    conn, _dom = env
    share = tmp_path / "share"
    share.mkdir()
    _script_capture(monkeypatch, [("0\n", "", 0), ("", "", 0), ("", "", 0)])
    mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")

    commands = _script_capture(
        monkeypatch, [("0\n", "", 0), ("", "", 0), ("", "", 0)]
    )
    with pytest.raises(MountError, match="already mounted"):
        mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")
    # 重复挂载不应触发任何 SSH 调用
    assert commands == []


# ---- unmount ----


def _mount_first(monkeypatch, env, tmp_path: Path) -> Path:
    conn, _dom = env
    share = tmp_path / "share"
    share.mkdir()
    _script_capture(monkeypatch, [("0\n", "", 0), ("", "", 0), ("", "", 0)])
    mounts.mount_instance(object(), conn, "vm1", str(share), "/mnt/share")
    return share


def test_unmount_runs_umount_and_detaches(env, tmp_path: Path, monkeypatch) -> None:
    conn, dom = env
    _mount_first(monkeypatch, env, tmp_path)
    commands = _script_capture(monkeypatch, [("0\n", "", 0), ("", "", 0)])

    rc = mounts.unmount_instance(object(), conn, "vm1", "/mnt/share")

    assert rc == 0
    assert commands == ["id -u", "umount /mnt/share"]
    assert len(dom.detached) == 1
    assert fsmod.list_mounts(dom.XMLDesc(0)) == []


def test_unmount_busy_keeps_device(env, tmp_path: Path, monkeypatch) -> None:
    conn, dom = env
    _mount_first(monkeypatch, env, tmp_path)
    detached_before = list(dom.detached)
    _script_capture(
        monkeypatch, [("0\n", "", 0), ("", "target is busy", 1)]
    )

    with pytest.raises(MountError, match="Cannot unmount"):
        mounts.unmount_instance(object(), conn, "vm1", "/mnt/share")

    # umount 失败时不得 detach
    assert dom.detached == detached_before
    assert fsmod.list_mounts(dom.XMLDesc(0))[0].target == "/mnt/share"


def test_unmount_unknown_path_raises(env, tmp_path: Path) -> None:
    conn, _dom = env
    with pytest.raises(MountError, match="No mounted host directory"):
        mounts.unmount_instance(object(), conn, "vm1", "/nope")


def test_unmount_shut_off_skips_guest_umount(
    env, tmp_path: Path, monkeypatch
) -> None:
    _conn, dom = env
    _mount_first(monkeypatch, env, tmp_path)

    # 重新构造一个已关机的域，保留已登记的持久 XML
    off_dom = FakeDom(dom.XMLDesc(0), state=libvirt.VIR_DOMAIN_SHUTOFF)
    off_conn = FakeConn(off_dom)
    monkeypatch.setattr(mounts.domains, "lookup_domain", lambda _c, _n: off_dom)
    commands = _script_capture(monkeypatch, [("0\n", "", 0)])

    assert (
        mounts.unmount_instance(object(), off_conn, "vm1", "/mnt/share") == 0
    )
    # 关机状态下不执行任何 guest 命令，但设备仍从持久配置移除
    assert commands == []
    assert fsmod.list_mounts(off_dom.XMLDesc(0)) == []
