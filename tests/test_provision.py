"""删除保护级别（level）在 destroy / create 编排层的行为测试。

全部 mock libvirt 与 stdin，不连接真实 hypervisor，也不依赖真实 TTY。
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import libvirt
import pytest

from kvm_cloud_init import provision
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ProtectionDenied, ProvisionError, SshError


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

    monkeypatch.setattr(provision.domains, "destroy_domain", fake_destroy)
    return recorded


def _existing_domain(monkeypatch) -> None:
    monkeypatch.setattr(
        provision.domains, "lookup_domain", lambda conn, name: object()
    )


# ---- _ensure_destroy_allowed 单元矩阵 ----


def test_guard_normal_always_allowed() -> None:
    provision._ensure_destroy_allowed(
        "x", "normal", skip_confirm=False, action="销毁"
    )


@pytest.mark.parametrize("skip_confirm", [False, True])
def test_guard_protected_always_denied(skip_confirm: bool) -> None:
    with pytest.raises(ProtectionDenied, match="protected"):
        provision._ensure_destroy_allowed(
            "x", "protected", skip_confirm=skip_confirm, action="销毁"
        )


def test_guard_production_accepts_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n"))
    provision._ensure_destroy_allowed(
        "x", "production", skip_confirm=False, action="销毁"
    )


def test_guard_production_accepts_yes_with_surrounding_space(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("  yes  \n"))
    provision._ensure_destroy_allowed(
        "x", "production", skip_confirm=False, action="销毁"
    )


def test_guard_production_rejects_wrong_answer(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("no\n"))
    with pytest.raises(ProtectionDenied, match="yes"):
        provision._ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="销毁"
        )


def test_guard_production_rejects_eof(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=True))
    with pytest.raises(ProtectionDenied):
        provision._ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="销毁"
        )


def test_guard_production_rejects_non_tty_even_piped_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n", tty=False))
    with pytest.raises(ProtectionDenied, match="非交互"):
        provision._ensure_destroy_allowed(
            "x", "production", skip_confirm=False, action="销毁"
        )


def test_guard_production_skip_confirm_on_non_tty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("yes\n", tty=False))
    provision._ensure_destroy_allowed(
        "x", "production", skip_confirm=True, action="销毁"
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
    with pytest.raises(ProtectionDenied, match="非交互"):
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

    monkeypatch.setattr(provision.cloudinit, "render_docs", fail_later)


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
    with pytest.raises(ProtectionDenied, match="非交互"):
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
        provision.domains, "lookup_domain", lambda conn, name: None
    )

    def fail_later(*args, **kwargs):
        raise _Proceeded

    monkeypatch.setattr(provision.cloudinit, "render_docs", fail_later)
    with pytest.raises(_Proceeded):
        provision.create_instance(level_store, object(), "crit")


# ---- destroy 同步清理密钥（R8） ----


def _record_key_deletion(monkeypatch) -> list[str]:
    deleted: list[str] = []

    def fake_delete(name: str) -> list:
        deleted.append(name)
        return []

    monkeypatch.setattr(provision.ssh, "delete_keypair", fake_delete)
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

    monkeypatch.setattr(provision.ssh, "delete_keypair", explode)
    assert provision.destroy_instance(level_store, object(), "plain") is True
    assert "警告" in capsys.readouterr().out


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
        provision.domains, "lookup_domain", lambda conn, name: _RunningDom()
    )
    monkeypatch.setattr(provision.guest, "domain_ssh_user", lambda dom: "cliff")
    monkeypatch.setattr(
        provision.network, "find_ipv4", lambda conn, name: "10.0.0.5"
    )
    key = tmp_path / "plain"
    key.write_text("private")
    monkeypatch.setattr(provision.ssh, "private_key_path", lambda name: key)
    return key


def test_shell_instance_passes_target(level_store, monkeypatch, tmp_path) -> None:
    key = _prepare_ssh_target(monkeypatch, tmp_path)
    captured = {}

    def fake_shell(instance, host, user, key_path) -> int:
        captured.update(instance=instance, host=host, user=user, key=key_path)
        return 0

    monkeypatch.setattr(provision.ssh, "interactive_shell", fake_shell)
    assert provision.shell_instance(level_store, object(), "plain") == 0
    assert captured == {"instance": "plain", "host": "10.0.0.5", "user": "cliff", "key": key}


def test_exec_instance_joins_command(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    captured = {}

    def fake_run(instance, host, user, key_path, command) -> int:
        captured.update(host=host, user=user, command=command)
        return 7

    monkeypatch.setattr(provision.ssh, "run_command", fake_run)
    rc = provision.exec_instance(level_store, object(), "plain", ["ls", "-l", "/"])
    assert rc == 7
    assert captured == {"host": "10.0.0.5", "user": "cliff", "command": "ls -l /"}


def test_shell_instance_requires_running_domain(level_store, monkeypatch) -> None:
    monkeypatch.setattr(
        provision.domains, "lookup_domain", lambda conn, name: None
    )
    with pytest.raises(ProvisionError, match="不存在"):
        provision.shell_instance(level_store, object(), "ghost")


def test_shell_instance_requires_recorded_user(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    monkeypatch.setattr(provision.guest, "domain_ssh_user", lambda dom: None)
    with pytest.raises(SshError, match="登录用户"):
        provision.shell_instance(level_store, object(), "plain")


def test_shell_instance_requires_private_key(level_store, monkeypatch, tmp_path) -> None:
    _prepare_ssh_target(monkeypatch, tmp_path)
    monkeypatch.setattr(
        provision.ssh, "private_key_path", lambda name: tmp_path / "missing"
    )
    with pytest.raises(SshError, match="私钥不存在"):
        provision.shell_instance(level_store, object(), "plain")


# ---- 等待 cloud-init 完成 ----

class _StoppedDom:
    def state(self, flags: int) -> list[int]:
        return [libvirt.VIR_DOMAIN_SHUTOFF]


def _prepare_wait(monkeypatch, *, dom=None) -> None:
    """等待流程的公共桩：运行中的域、已就绪的 IP、无按键、无真实等待。"""
    monkeypatch.setattr(
        provision.domains,
        "lookup_domain",
        lambda conn, name: _RunningDom() if dom is None else dom,
    )
    monkeypatch.setattr(
        provision.network, "probe_ipv4", lambda conn, name: "10.0.0.5"
    )
    monkeypatch.setattr(provision, "_key_pressed", lambda timeout: False)


def test_cloud_init_status_parsing() -> None:
    assert (
        provision._cloud_init_status("status: done\nextended_status: done\n")
        == "done"
    )
    assert provision._cloud_init_status("status: running\n") == "running"
    assert provision._cloud_init_status("detail: no status here\n") is None


def test_key_pressed_non_tty_just_sleeps(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(provision.sys, "stdin", _FakeStdin("", tty=False))
    monkeypatch.setattr(provision.time, "sleep", sleeps.append)

    assert provision._key_pressed(0.2) is False
    assert sleeps == [0.2]


def test_wait_cloud_init_reports_done(monkeypatch, capsys) -> None:
    _prepare_wait(monkeypatch)
    captured = {}

    def fake_capture(instance, host, user, key_path, command):
        captured.update(host=host, user=user, command=command)
        return 0, "status: done\n"

    monkeypatch.setattr(provision.ssh, "run_capture", fake_capture)
    key = Path("/keys/plain")

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=key
    ) is True
    assert captured == {
        "host": "10.0.0.5",
        "user": "cliff",
        "command": "cloud-init status",
    }
    assert "cloud-init 已完成" in capsys.readouterr().out


def test_wait_cloud_init_polls_until_done(monkeypatch) -> None:
    _prepare_wait(monkeypatch)
    responses = iter([(2, "status: running\n"), (0, "status: done\n")])
    monkeypatch.setattr(provision.ssh, "run_capture", lambda *args: next(responses))

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is True


def test_wait_cloud_init_waits_for_ip(monkeypatch) -> None:
    _prepare_wait(monkeypatch)
    ips = iter([None, "10.0.0.5"])
    monkeypatch.setattr(
        provision.network, "probe_ipv4", lambda conn, name: next(ips)
    )
    monkeypatch.setattr(
        provision.ssh, "run_capture", lambda *args: (0, "status: done\n")
    )

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is True


def test_wait_cloud_init_retries_unreachable_ssh(monkeypatch) -> None:
    _prepare_wait(monkeypatch)
    attempts = {"count": 0}

    def fake_capture(*args) -> tuple[int, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise SshError("sshd 尚未就绪")
        return 0, "status: done\n"

    monkeypatch.setattr(provision.ssh, "run_capture", fake_capture)

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is True
    assert attempts["count"] == 2


def test_wait_cloud_init_reports_error_state(monkeypatch, capsys) -> None:
    _prepare_wait(monkeypatch)
    monkeypatch.setattr(
        provision.ssh, "run_capture", lambda *args: (2, "status: error\n")
    )

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is True
    out = capsys.readouterr().out
    assert "状态为 error" in out
    assert "kvmci shell plain" in out


def test_wait_cloud_init_skips_when_command_missing(monkeypatch, capsys) -> None:
    _prepare_wait(monkeypatch)
    monkeypatch.setattr(provision.ssh, "run_capture", lambda *args: (127, ""))

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is True
    assert "未找到 cloud-init" in capsys.readouterr().out


def test_wait_cloud_init_keypress_skips(monkeypatch, capsys) -> None:
    _prepare_wait(monkeypatch)
    monkeypatch.setattr(provision, "_key_pressed", lambda timeout: timeout == 0)
    monkeypatch.setattr(
        provision.ssh, "run_capture", lambda *args: (0, "status: running\n")
    )

    assert provision.wait_for_cloud_init(
        object(), "plain", ssh_user="cliff", key_path=Path("/k")
    ) is False
    assert "已跳过等待" in capsys.readouterr().out


def test_wait_cloud_init_stops_when_domain_not_running(monkeypatch) -> None:
    _prepare_wait(monkeypatch, dom=_StoppedDom())

    with pytest.raises(ProvisionError, match="未处于运行状态"):
        provision.wait_for_cloud_init(
            object(), "plain", ssh_user="cliff", key_path=Path("/k")
        )


# ---- create 与等待流程的衔接 ----


def _stub_create_pipeline(monkeypatch, tmp_path: Path) -> None:
    """把创建流程中除“启动”外的全部副作用替换为桩。"""
    monkeypatch.setattr(
        provision.domains, "lookup_domain", lambda conn, name: None
    )
    monkeypatch.setattr(
        provision.cloudinit,
        "render_docs",
        lambda store, resolved: {
            "meta-data": "instance-id: plain\n",
            "user-data": "#cloud-config\n",
        },
    )
    monkeypatch.setattr(provision.ssh, "ensure_keypair", lambda name: tmp_path / name)
    monkeypatch.setattr(provision.ssh, "read_public_key", lambda name: "ssh-ed25519 AAAA")
    monkeypatch.setattr(
        provision.cloudinit,
        "inject_ssh_access",
        lambda user_data, public_key: (user_data, "cliff"),
    )
    monkeypatch.setattr(provision.ssh, "private_key_path", lambda name: tmp_path / name)
    monkeypatch.setattr(
        provision.client, "get_pool", lambda conn, name, refresh=True: object()
    )
    monkeypatch.setattr(provision, "pool_target_path", lambda pool: str(tmp_path))
    monkeypatch.setattr(provision.hv_storage, "delete_path", lambda *args: None)
    monkeypatch.setattr(
        provision.qcow2, "read_info", lambda path: SimpleNamespace(virtual_size=1024)
    )

    @contextmanager
    def fake_seed(docs):
        seed = tmp_path / "cidata.iso"
        seed.write_bytes(b"seed")
        yield seed

    monkeypatch.setattr(provision.seediso, "temporary_seed", fake_seed)
    monkeypatch.setattr(
        provision.hv_storage, "upload_file", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        provision.firmware, "choose_firmware", lambda firmware, conn: "bios"
    )
    monkeypatch.setattr(
        provision.guest, "build_domain_xml", lambda **kwargs: "<domain/>"
    )
    monkeypatch.setattr(
        provision.guest, "define_and_start", lambda conn, xml: None
    )


def test_create_waits_for_cloud_init_by_default(level_store, monkeypatch, tmp_path) -> None:
    _stub_create_pipeline(monkeypatch, tmp_path)
    calls = {}

    def fake_wait(conn, name, *, ssh_user, key_path) -> bool:
        calls.update(name=name, ssh_user=ssh_user, key_path=key_path)
        return True

    monkeypatch.setattr(provision, "wait_for_cloud_init", fake_wait)

    provision.create_instance(level_store, object(), "plain")

    assert calls == {
        "name": "plain",
        "ssh_user": "cliff",
        "key_path": tmp_path / "plain",
    }


def test_create_no_wait_skips_cloud_init(level_store, monkeypatch, tmp_path) -> None:
    _stub_create_pipeline(monkeypatch, tmp_path)
    called: list[str] = []
    monkeypatch.setattr(
        provision, "wait_for_cloud_init", lambda *args, **kwargs: called.append("wait")
    )

    provision.create_instance(level_store, object(), "plain", wait=False)

    assert called == []
