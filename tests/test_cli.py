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
    assert "Rocky-LLM" in capsys.readouterr().out


def test_create_dispatch(monkeypatch) -> None:
    calls = {}

    def fake_create(store, conn, name, template_name=None):
        calls.update(name=name, template=template_name)

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "create_instance", fake_create)

    assert cli.main(["create", "rocky-llm"]) == 0
    assert calls == {"name": "rocky-llm", "template": None}

    assert cli.main(["create", "adhoc", "--template", "Debian"]) == 0
    assert calls == {"name": "adhoc", "template": "Debian"}


def test_destroy_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "destroy_instance",
        lambda store, conn, name, force: calls.update(name=name, force=force),
    )

    assert cli.main(["destroy", "rocky-llm"]) == 0
    assert calls == {"name": "rocky-llm", "force": False}

    assert cli.main(["destroy", "rocky-llm", "--force"]) == 0
    assert calls == {"name": "rocky-llm", "force": True}


def test_list_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    assert cli.main(["list"]) == 0
    assert "rocky-llm" in capsys.readouterr().out


def test_unknown_instance_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    rc = cli.main(["create", "not-exist"])
    assert rc == 1
    assert "错误" in capsys.readouterr().err


def test_bad_args() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code == 2
