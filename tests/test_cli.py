"""CLI 参数解析与调度测试（不连接真实 libvirt）。"""

from __future__ import annotations

import pytest

from kvm_cloud_init import cli


class _FakeConn:
    def listAllDomains(self, flags: int = 0) -> list:
        return []


def test_templates_command(capsys) -> None:
    rc = cli.main(["templates"])
    assert rc == 0
    assert "Rocky-LVM" in capsys.readouterr().out


def test_create_dispatch(monkeypatch) -> None:
    calls = {}

    def fake_create(store, conn, name, template_name=None, yes=False, force=False):
        calls.update(name=name, template=template_name, yes=yes, force=force)

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "create_instance", fake_create)

    assert cli.main(["create", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm", "template": None, "yes": False, "force": False}

    assert cli.main(["create", "adhoc", "--template", "Debian"]) == 0
    assert calls == {"name": "adhoc", "template": "Debian", "yes": False, "force": False}


def test_create_yes_and_force_dispatch(monkeypatch) -> None:
    calls = {}

    def fake_create(store, conn, name, template_name=None, yes=False, force=False):
        calls.update(yes=yes, force=force)

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "create_instance", fake_create)

    cli.main(["create", "rocky-lvm", "--yes"])
    assert calls == {"yes": True, "force": False}

    cli.main(["create", "rocky-lvm", "--force"])
    assert calls == {"yes": False, "force": True}

    cli.main(["create", "rocky-lvm", "--yes", "--force"])
    assert calls == {"yes": True, "force": True}


def test_destroy_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "destroy_instance",
        lambda store, conn, name, force, yes: calls.update(
            name=name, force=force, yes=yes
        ),
    )

    assert cli.main(["destroy", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm", "force": False, "yes": False}

    assert cli.main(["destroy", "rocky-lvm", "--force"]) == 0
    assert calls == {"name": "rocky-lvm", "force": True, "yes": False}


def test_destroy_yes_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "destroy_instance",
        lambda store, conn, name, force, yes: calls.update(force=force, yes=yes),
    )

    assert cli.main(["destroy", "rocky-lvm", "--yes"]) == 0
    assert calls == {"force": False, "yes": True}

    assert cli.main(["destroy", "rocky-lvm", "--yes", "--force"]) == 0
    assert calls == {"force": True, "yes": True}


def test_list_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    assert cli.main(["list"]) == 0
    assert "rocky-lvm" in capsys.readouterr().out


def test_unknown_instance_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    rc = cli.main(["create", "not-exist"])
    assert rc == 1
    assert "错误" in capsys.readouterr().err


def test_bad_args() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code == 2
