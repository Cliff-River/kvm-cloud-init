"""删除保护级别（level）在 destroy / create 编排层的行为测试。

全部 mock libvirt 与 stdin，不连接真实 hypervisor，也不依赖真实 TTY。
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path

import libvirt
import pytest

from kvm_cloud_init import cloudinit, provision, ssh
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ProtectionDenied, ProvisionError, SshError
from kvm_cloud_init.hv import domains, guest, network


class _FakeStdin:
    """可控制 isatty/readline 的假 stdin（禁止依赖真实 TTY）。"""

    def __init__(self, text: str = "", *, tty: bool = True) -> None:
        self._buf = io.StringIO(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._buf.readline()


class _Proceeded(Exception):
    """保护检查通过、流程继续向下执行时由 mock 抛出的哨兵异常。"""


@pytest.fixture
def level_store(project_dir: Path) -> ConfigStore:
    (project_dir / "images" / "base.qcow2").write_bytes(b"")
    (project_dir / "instances.conf").write_text(
        "prod:\n  template: t1\n  level: production\n"
        "crit:\n  template: t1\n  level: protected\n"
        "plain:\n  template: t1\n"
    )
    return ConfigStore.load(project_dir)


def _record_destroy(monkeypatch) -> list[tuple[tuple, dict]]:
    recorded: list[tuple[tuple, dict]] = []

    def fake_destroy(*args, **kwargs) -> bool:
        recorded.append((args, kwargs))
        return True

    monkeypatch.setattr(domains, "destroy_domain", fake_destroy)
    return recorded


def _existing_domain(monkeypatch) -> None:
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: object()
    )


# ---- _ensure_destroy_allowed 单元矩阵 ----


def test_guard_normal_always_allowed() -> None:
    provision.ensure_destroy_allowed(
        "x", "normal", skip_confirm=False, action="destroy"
    )


@pytest.mark.parametrize("skip_confirm", [False, True])
def test_guard_protected_always_denied(skip_confirm: bool) -> None:
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.ensure_destroy_allowed(
            "x", "protected", skip_confirm=skip_confirm, action="destroy"
        )


def test_guard_production_accepts_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n"))
    provision.ensure_destroy_allowed(
        "x", "production", skip_confirm=False, action="destroy"
    )


def test_guard_production_accepts_yes_with_surrounding_space(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("  yes  \n"))
    provision.ensure_destroy_allowed(
        "x", "production", skip_confirm=False, action="destroy"
    )


def test_guard_production_rejects_wrong_answer(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("no\n"))
    with pytest.raises(ProtectionDenied, match="yes"):
        provision.ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="destroy"
        )


def test_guard_production_rejects_eof(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=True))
    with pytest.raises(ProtectionDenied):
        provision.ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="destroy"
        )


def test_guard_production_rejects_non_tty_even_piped_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n", tty=False))
    with pytest.raises(ProtectionDenied, match="non-interactive"):
        provision.ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="destroy"
        )


def test_guard_production_skip_confirm_on_non_tty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n", tty=False))
    provision.ensure_destroy_allowed(
        "x", "production", skip_confirm=True, action="destroy"
    )


# ---- destroy_instance ----


def test_destroy_protected_denied(level_store, monkeypatch) -> None:
    recorded = _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.destroy_instance(level_store, object(), "crit")
    assert recorded == []


def test_destroy_protected_yes_still_denied(level_store, monkeypatch) -> None:
    _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.destroy_instance(level_store, object(), "crit", yes=True)


def test_destroy_production_non_tty_denied(level_store, monkeypatch) -> None:
    recorded = _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="non-interactive"):
        provision.destroy_instance(level_store, object(), "prod")
    assert recorded == []


def test_destroy_production_confirmed_interactively(
    level_store, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n"))
    _record_destroy(monkeypatch)
    assert provision.destroy_instance(level_store, object(), "prod") is True


def test_destroy_production_yes_flag(level_store, monkeypatch) -> None:
    recorded = _record_destroy(monkeypatch)
    assert provision.destroy_instance(level_store, object(), "prod", yes=True) is True
    assert len(recorded) == 1


def test_destroy_unregistered_name_treated_as_normal(
    level_store, monkeypatch
) -> None:
    # 未登记实例无 level 配置：非 TTY 下也应直接放行，不弹确认
    recorded = _record_destroy(monkeypatch)
    assert provision.destroy_instance(level_store, object(), "ghost") is True
    assert len(recorded) == 1


# ---- create_instance 的隐式销毁 ----


def _proceed_after_destroy(monkeypatch, recorded) -> None:
    """模拟“旧域已销毁、继续重建流程”：render_docs 处抛出哨兵。"""
    _existing_domain(monkeypatch)

    def fail_later(*args, **kwargs) -> Callable:
        raise _Proceeded

    monkeypatch.setattr(cloudinit, "render_docs", fail_later)


def test_create_protected_existing_domain_denied(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    recorded = _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.create_instance(level_store, object(), "crit")
    assert recorded == []


def test_create_protected_force_still_denied(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.create_instance(level_store, object(), "crit", force=True)


def test_create_protected_yes_still_denied(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.create_instance(level_store, object(), "crit", yes=True)


def test_create_production_non_tty_denied(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    recorded = _record_destroy(monkeypatch)
    with pytest.raises(ProtectionDenied, match="non-interactive"):
        provision.create_instance(level_store, object(), "prod")
    assert recorded == []


def test_create_production_force_passes_guard(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    recorded = _record_destroy(monkeypatch)
    _proceed_after_destroy(monkeypatch, recorded)
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "prod", force=True)
    assert len(recorded) == 1
    # create 的 --force 只跳过 production 确认；隐式销毁仍走优雅关机
    assert recorded[0][1] == {"force": False}


def test_create_production_yes_flag_passes_guard(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    recorded = _record_destroy(monkeypatch)
    _proceed_after_destroy(monkeypatch, recorded)
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "prod", yes=True)
    assert len(recorded) == 1


def test_create_production_confirmed_interactively(
    level_store, monkeypatch
) -> None:
    _existing_domain(monkeypatch)
    recorded = _record_destroy(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n"))
    _proceed_after_destroy(monkeypatch, recorded)
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "prod")
    assert len(recorded) == 1


def test_create_protected_without_existing_domain_allowed(
    level_store, monkeypatch
) -> None:
    # 无同名域时不存在销毁动作，protected 不影响新建
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: None
    )

    def fail_later(*args, **kwargs):
        raise _Proceeded

    monkeypatch.setattr(cloudinit, "render_docs", fail_later)
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "crit")


# ---- destroy 同步清理密钥（R8） ----


def _record_key_deletion(monkeypatch) -> list[str]:
    deleted: list[str] = []

    def fake_delete(name: str) -> list:
        deleted.append(name)
        return []

    monkeypatch.setattr(ssh, "delete_keypair", fake_delete)
    return deleted


def test_destroy_allowed_removes_keypair(level_store, monkeypatch) -> None:
    _record_destroy(monkeypatch)
    deleted = _record_key_deletion(monkeypatch)
    assert provision.destroy_instance(level_store, object(), "plain") is True
    assert deleted == ["plain"]


def test_destroy_protected_keeps_keypair(level_store, monkeypatch) -> None:
    deleted = _record_key_deletion(monkeypatch)
    with pytest.raises(ProtectionDenied, match="protected"):
        provision.destroy_instance(level_store, object(), "crit")
    assert deleted == []


def test_destroy_key_deletion_failure_does_not_block(level_store, monkeypatch, capsys) -> None:
    _record_destroy(monkeypatch)

    def explode(name: str) -> list:
        raise OSError("权限不足")

    monkeypatch.setattr(ssh, "delete_keypair", explode)
    assert provision.destroy_instance(level_store, object(), "plain") is True
    assert "Warning" in capsys.readouterr().out


def test_create_rebuild_does_not_delete_keypair(level_store, monkeypatch) -> None:
    _existing_domain(monkeypatch)
    _record_destroy(monkeypatch)
    deleted = _record_key_deletion(monkeypatch)
    _proceed_after_destroy(monkeypatch, [])
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "plain", yes=True)
    assert deleted == []


# ---- shell / exec 的目标解析 ----


class _RunningDom:
    def state(self, flags: int) -> list[int]:
        return [libvirt.VIR_DOMAIN_RUNNING]


def _prepare_ssh_target(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: _RunningDom()
    )
    monkeypatch.setattr(guest, "domain_ssh_user", lambda dom: "cliff")
    monkeypatch.setattr(
        network, "find_ipv4", lambda conn, name: "10.0.0.5"
    )
    key = tmp_path / "plain"
    key.write_text("private")
    monkeypatch.setattr(ssh, "private_key_path", lambda name: key)
    return key


def test_shell_instance_passes_target(level_store, monkeypatch, tmp_path) -> None:
    key = _prepare_ssh_target(monkeypatch, tmp_path)
    captured = {}

    def fake_shell(instance, host, user, key_path) -> int:
        captured.update(instance=instance, host=host, user=user, key=key_path)
        return 0

    monkeypatch.setattr(ssh, "interactive_shell", fake_shell)
    assert provision.shell_instance(level_store, object(), "plain") == 0
    assert captured == {"instance": "plain", "host": "10.0.0.5", "user": "cliff", "key": key}


def test_exec_instance_joins_command(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    captured = {}

    def fake_run(instance, host, user, key_path, command) -> int:
        captured.update(host=host, user=user, command=command)
        return 7

    monkeypatch.setattr(ssh, "run_command", fake_run)
    rc = provision.exec_instance(level_store, object(), "plain", ["ls", "-l", "/"])
    assert rc == 7
    assert captured == {"host": "10.0.0.5", "user": "cliff", "command": "ls -l /"}


def test_shell_instance_requires_running_domain(level_store, monkeypatch) -> None:
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: None
    )
    with pytest.raises(ProvisionError, match="does not exist"):
        provision.shell_instance(level_store, object(), "ghost")


def test_shell_instance_auto_starts_shutoff_domain(
    level_store, monkeypatch, tmp_path
) -> None:
    from kvm_cloud_init.provision import access

    class _ShutoffDom:
        def state(self, flags: int) -> list[int]:
            return [libvirt.VIR_DOMAIN_SHUTOFF]

    key = tmp_path / "plain"
    key.write_text("private")
    started: list[str] = []
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: _ShutoffDom()
    )
    monkeypatch.setattr(
        access.lifecycle,
        "start_instance",
        lambda store, conn, name: started.append(name),
    )
    monkeypatch.setattr(
        access, "ssh_target", lambda conn, name: ("10.0.0.5", "cliff", key)
    )
    monkeypatch.setattr(ssh, "interactive_shell", lambda *args: 0)

    assert provision.shell_instance(level_store, object(), "plain") == 0
    assert started == ["plain"]


def test_shell_instance_does_not_restart_running_domain(
    level_store, monkeypatch, tmp_path
) -> None:
    from kvm_cloud_init.provision import access

    key = _prepare_ssh_target(monkeypatch, tmp_path)
    started: list[str] = []
    monkeypatch.setattr(
        access.lifecycle,
        "start_instance",
        lambda store, conn, name: started.append(name),
    )
    monkeypatch.setattr(ssh, "interactive_shell", lambda *args: 0)

    assert provision.shell_instance(level_store, object(), "plain") == 0
    assert started == []
    assert key.exists()


def test_wait_ssh_target_retries_until_ip(monkeypatch, tmp_path) -> None:
    from kvm_cloud_init.errors import NetworkError
    from kvm_cloud_init.provision import access

    key = tmp_path / "plain"
    key.write_text("private")
    target = ("10.0.0.5", "cliff", key)
    results: list[object] = [
        NetworkError("no IP"),
        NetworkError("no IP"),
        target,
    ]

    def fake_target(conn, name):
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(access, "ssh_target", fake_target)
    monkeypatch.setattr(access.time, "sleep", lambda _seconds: None)
    assert access.wait_ssh_target(object(), "plain") == target


def test_exec_instance_auto_starts_shutoff_domain(
    level_store, monkeypatch, tmp_path
) -> None:
    from kvm_cloud_init.provision import access

    class _ShutoffDom:
        def state(self, flags: int) -> list[int]:
            return [libvirt.VIR_DOMAIN_SHUTOFF]

    key = tmp_path / "plain"
    key.write_text("private")
    started: list[str] = []
    captured = {}
    monkeypatch.setattr(
        domains, "lookup_domain", lambda conn, name: _ShutoffDom()
    )
    monkeypatch.setattr(
        access.lifecycle,
        "start_instance",
        lambda store, conn, name: started.append(name),
    )
    monkeypatch.setattr(
        access, "ssh_target", lambda conn, name: ("10.0.0.5", "cliff", key)
    )

    def fake_run(instance, host, user, key_path, command) -> int:
        captured.update(host=host, command=command)
        return 0

    monkeypatch.setattr(ssh, "run_command", fake_run)

    rc = provision.exec_instance(level_store, object(), "plain", ["hostname"])
    assert rc == 0
    assert started == ["plain"]
    assert captured == {"host": "10.0.0.5", "command": "hostname"}


def test_shell_instance_requires_recorded_user(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    monkeypatch.setattr(guest, "domain_ssh_user", lambda dom: None)
    with pytest.raises(SshError, match="SSH login user"):
        provision.shell_instance(level_store, object(), "plain")


def test_shell_instance_requires_private_key(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ssh, "private_key_path", lambda name: tmp_path / "missing"
    )
    with pytest.raises(SshError, match="Private key"):
        provision.shell_instance(level_store, object(), "plain")
