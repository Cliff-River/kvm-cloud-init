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

    def fake_create(
        store,
        conn,
        name,
        template_name=None,
        yes=False,
        force=False,
        no_wait=False,
        verbose=False,
        local=False,
    ):
        calls.update(
            name=name,
            template=template_name,
            yes=yes,
            force=force,
            no_wait=no_wait,
            verbose=verbose,
            local=local,
        )
        return 0

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "create_instance", fake_create)

    assert cli.main(["create", "rocky-lvm"]) == 0
    assert calls == {
        "name": "rocky-lvm",
        "template": None,
        "yes": False,
        "force": False,
        "no_wait": False,
        "verbose": False,
        "local": False,
    }

    assert cli.main(["create", "adhoc", "--template", "Debian"]) == 0
    assert calls == {
        "name": "adhoc",
        "template": "Debian",
        "yes": False,
        "force": False,
        "no_wait": False,
        "verbose": False,
        "local": False,
    }


def test_create_wait_flags_dispatch(monkeypatch) -> None:
    calls = {}

    def fake_create(
        store,
        conn,
        name,
        template_name=None,
        yes=False,
        force=False,
        no_wait=False,
        verbose=False,
        local=False,
    ):
        calls.update(no_wait=no_wait, verbose=verbose)
        return 0

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "create_instance", fake_create)

    assert cli.main(["create", "rocky-lvm", "--no-wait"]) == 0
    assert calls == {"no_wait": True, "verbose": False}

    assert cli.main(["create", "rocky-lvm", "--verbose"]) == 0
    assert calls == {"no_wait": False, "verbose": True}


def test_create_returns_wait_failure_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision, "create_instance", lambda *args, **kwargs: 1
    )
    assert cli.main(["create", "rocky-lvm"]) == 1


def test_create_yes_and_force_dispatch(monkeypatch) -> None:
    calls = {}

    def fake_create(
        store,
        conn,
        name,
        template_name=None,
        yes=False,
        force=False,
        no_wait=False,
        verbose=False,
        local=False,
    ):
        calls.update(yes=yes, force=force)
        return 0

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
    assert "Error" in capsys.readouterr().err


def test_bad_args() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code == 2


def test_keyboard_interrupt_reports_cancelled(monkeypatch, capsys) -> None:
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(cli.provision, "list_instances", interrupted)

    assert cli.main(["list"]) == 130
    captured = capsys.readouterr()
    assert "Operation cancelled." in captured.err


def test_shell_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "shell_instance",
        lambda store, conn, name: calls.update(name=name) or 0,
    )
    assert cli.main(["shell", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm"}


def test_exec_dispatch_joins_command_after_separator(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "exec_instance",
        lambda store, conn, name, command: calls.update(
            name=name, command=command
        ) or 7,
    )
    assert cli.main(["exec", "rocky-lvm", "--", "ls", "-l", "/"]) == 7
    assert calls == {"name": "rocky-lvm", "command": ["ls", "-l", "/"]}


def test_exec_without_command_is_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    assert cli.main(["exec", "rocky-lvm"]) == 2
    assert "remote command" in capsys.readouterr().err


def test_start_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "start_instance",
        lambda store, conn, name: calls.update(name=name),
    )
    assert cli.main(["start", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm"}


def test_stop_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "stop_instance",
        lambda store, conn, name, force=False: calls.update(
            name=name, force=force
        ),
    )
    assert cli.main(["stop", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm", "force": False}

    assert cli.main(["stop", "rocky-lvm", "--force"]) == 0
    assert calls == {"name": "rocky-lvm", "force": True}


def test_reboot_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "reboot_instance",
        lambda store, conn, name: calls.update(name=name),
    )
    assert cli.main(["reboot", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm"}


def test_status_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "status_instance",
        lambda store, conn, name: calls.update(name=name),
    )
    assert cli.main(["status", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm"}


def test_cloud_init_status_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "cloud_init_status",
        lambda conn, name: calls.update(name=name) or 2,
    )
    assert cli.main(["cloud-init", "status", "rocky-lvm"]) == 2
    assert calls == {"name": "rocky-lvm"}


def test_cloud_init_log_dispatch(monkeypatch) -> None:
    calls = {}
    monkeypatch.setattr(cli, "connect", lambda: _FakeConn())
    monkeypatch.setattr(
        cli.provision,
        "cloud_init_log",
        lambda conn, name, follow=False: calls.update(
            name=name, follow=follow
        )
        or 0,
    )
    assert cli.main(["cloud-init", "log", "rocky-lvm"]) == 0
    assert calls == {"name": "rocky-lvm", "follow": False}

    assert cli.main(["cloud-init", "log", "rocky-lvm", "-f"]) == 0
    assert calls == {"name": "rocky-lvm", "follow": True}


def test_cloud_init_requires_subcommand() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["cloud-init"])
    assert exc.value.code == 2


# ---- images 子命令 ----


@pytest.fixture
def use_project(project_dir, monkeypatch):
    """把 images 子命令的项目根指向临时项目，避免读取真实仓库配置。"""
    monkeypatch.setattr(cli, "project_root", lambda: project_dir)
    return project_dir


def test_images_list_does_not_connect(use_project, monkeypatch, capsys) -> None:
    def fail() -> object:
        raise AssertionError("images must not connect to libvirt")

    monkeypatch.setattr(cli, "connect", fail)
    assert cli.main(["images", "list"]) == 0
    out = capsys.readouterr().out
    assert "Image ID" in out
    assert "base" in out


def test_images_init_existing_and_force(use_project, capsys) -> None:
    assert cli.main(["images", "init"]) == 1
    assert "already exists" in capsys.readouterr().err

    assert cli.main(["images", "init", "--force"]) == 0
    assert "Generated image catalog" in capsys.readouterr().out


def test_images_refresh_dispatch(use_project, monkeypatch) -> None:
    calls = {}

    def fake_refresh(root, *, include_archived=False):
        calls.update(root=root, include_archived=include_archived)
        return 0

    monkeypatch.setattr(cli.provision, "refresh_catalog", fake_refresh)
    assert cli.main(["images", "refresh", "--include-archived"]) == 0
    assert calls == {"root": use_project, "include_archived": True}


def test_images_check_dispatch(use_project, monkeypatch) -> None:
    calls = {}

    def fake_check(store, tokens, *, all_entries=False, include_archived=False):
        calls.update(tokens=tokens, all_entries=all_entries)
        return 1

    monkeypatch.setattr(cli.provision, "check_images", fake_check)
    assert cli.main(["images", "check", "base", "t2", "--include-archived"]) == 1
    assert calls == {"tokens": ["base", "t2"], "all_entries": False}


def test_images_pull_requires_ids_or_all(use_project, capsys) -> None:
    assert cli.main(["images", "pull"]) == 2
    assert "image ID or --all" in capsys.readouterr().err


def test_images_pull_dispatch(use_project, monkeypatch) -> None:
    calls = {}

    def fake_pull(store, tokens, *, all_templates=False, force=False):
        calls.update(tokens=tokens, all_templates=all_templates, force=force)
        return 0

    monkeypatch.setattr(cli.provision, "pull_images", fake_pull)
    assert cli.main(["images", "pull", "--all", "--force"]) == 0
    assert calls == {"tokens": [], "all_templates": True, "force": True}
