"""删除保护级别（level）在 destroy / create 编排层的行为测试。

全部 mock libvirt 与 stdin，不连接真实 hypervisor，也不依赖真实 TTY。
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kvm_cloud_init import provision
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ProtectionDenied


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
