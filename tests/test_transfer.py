"""``cp`` 编排测试：路径方向解析与 SFTP 上/下行（全部 mock SSH）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kvm_cloud_init.errors import ProvisionError
from kvm_cloud_init.provision.files import cp, paths


def test_parse_remote() -> None:
    remote = paths.parse_remote("vm1:/etc/hosts")
    assert remote is not None
    assert remote.instance == "vm1"
    assert remote.path == "/etc/hosts"
    assert paths.parse_remote("./local.txt") is None
    assert paths.parse_remote("vm1:") is None
    assert paths.parse_remote(":/etc/hosts") is None


@pytest.fixture
def ssh_ready(monkeypatch):
    """把实例存在性、自动启动与 SSH 就绪等待全部替换为内存桩。"""
    monkeypatch.setattr(
        paths.domains, "lookup_domain", lambda _conn, _name: object()
    )
    monkeypatch.setattr(paths.access, "_ensure_running", lambda *a: None)
    monkeypatch.setattr(
        paths.access,
        "wait_ssh_target",
        lambda _conn, _name, *, deadline=None: (
            "10.0.0.5",
            "cliff",
            Path("/keys/vm1"),
        ),
    )
    return monkeypatch


def test_copy_rejects_two_remote_sides(monkeypatch) -> None:
    with pytest.raises(ProvisionError, match="between two instances"):
        cp.copy_file(object(), object(), "a:/x", "b:/y")


def test_copy_rejects_two_local_sides(monkeypatch) -> None:
    with pytest.raises(ProvisionError, match="instance path"):
        cp.copy_file(object(), object(), "./a", "./b")


def test_upload_uses_sftp_with_resolved_target(ssh_ready, tmp_path: Path, monkeypatch) -> None:
    local = tmp_path / "app.conf"
    local.write_text("config")
    calls = []
    monkeypatch.setattr(
        cp.ssh, "remote_is_directory", lambda *a, **k: False
    )

    def fake_upload(instance, host, user, key_path, src, dst, *, deadline=None):
        calls.append((instance, host, user, str(key_path), src, dst))
        return 1

    monkeypatch.setattr(cp.ssh, "upload_path", fake_upload)

    rc = cp.copy_file(object(), object(), str(local), "vm1:/etc/app.conf")

    assert rc == 0
    assert calls == [
        ("vm1", "10.0.0.5", "cliff", "/keys/vm1", local, "/etc/app.conf")
    ]


def test_upload_into_remote_directory_appends_basename(
    ssh_ready, tmp_path: Path, monkeypatch, capsys
) -> None:
    local = tmp_path / "app.conf"
    local.write_text("x")
    calls = []
    monkeypatch.setattr(
        cp.ssh, "remote_is_directory", lambda *a, **k: True
    )
    monkeypatch.setattr(
        cp.ssh,
        "upload_path",
        lambda *a, **k: calls.append((a[4], a[5])) or 1,
    )

    assert cp.copy_file(object(), object(), str(local), "vm1:/etc") == 0
    assert calls == [(local, "/etc/app.conf")]
    assert "vm1:/etc/app.conf" in capsys.readouterr().out


def test_upload_directory_dispatches_recursive_path(
    ssh_ready, tmp_path: Path, monkeypatch
) -> None:
    local_dir = tmp_path / "kube"
    local_dir.mkdir()
    calls = []
    monkeypatch.setattr(
        cp.ssh, "remote_is_directory", lambda *a, **k: False
    )
    monkeypatch.setattr(
        cp.ssh,
        "upload_path",
        lambda *a, **k: calls.append((a[4], a[5])) or 3,
    )

    assert (
        cp.copy_file(object(), object(), str(local_dir), "vm1:/srv/kube")
        == 0
    )
    assert calls == [(local_dir, "/srv/kube")]


def test_upload_missing_local_path_raises(ssh_ready, tmp_path: Path) -> None:
    with pytest.raises(ProvisionError, match="Local path not found"):
        cp.copy_file(
            object(), object(), str(tmp_path / "missing"), "vm1:/etc/x"
        )


def test_upload_unknown_instance_raises(monkeypatch, tmp_path: Path) -> None:
    local = tmp_path / "a"
    local.write_text("x")
    monkeypatch.setattr(
        paths.domains, "lookup_domain", lambda _conn, _name: None
    )
    with pytest.raises(ProvisionError, match="does not exist"):
        cp.copy_file(object(), object(), str(local), "ghost:/etc/x")


def test_download_uses_sftp_and_local_dir_target(
    ssh_ready, tmp_path: Path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        cp.ssh,
        "download_path",
        lambda *a, **k: calls.append((a[0], a[4], a[5])) or 1,
    )

    # 本地目标给的是已存在目录：落入目录并沿用远端文件名
    assert (
        cp.copy_file(
            object(), object(), "vm1:/var/log/app.log", str(tmp_path)
        )
        == 0
    )
    assert calls == [("vm1", "/var/log/app.log", tmp_path / "app.log")]

    # 本地目标给的是完整文件路径：原样使用
    assert (
        cp.copy_file(
            object(), object(), "vm1:/var/log/app.log", str(tmp_path / "x.log")
        )
        == 0
    )
    assert calls[-1] == ("vm1", "/var/log/app.log", tmp_path / "x.log")
