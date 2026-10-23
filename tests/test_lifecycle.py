"""start / stop / reboot / status 编排测试（全部 mock，不连接真实 libvirt）。"""

from __future__ import annotations

import contextlib
from pathlib import Path

import libvirt
import pytest

from kvm_cloud_init import provision
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import NetworkError, ProvisionError, ShutdownTimeout
from kvm_cloud_init.hv import domains, network


class _Dom:
    def __init__(
        self,
        state: int,
        *,
        memory_kib: int = 2048,
        vcpus: int = 2,
        cpu_time: int = 0,
    ) -> None:
        self._state = state
        self._info = [
            state,
            memory_kib,
            memory_kib,
            vcpus,
            cpu_time,
        ]
        self.create_calls = 0
        self.reboot_calls = 0

    def state(self, flags: int = 0) -> list[int]:
        return [self._state, 1]

    def info(self) -> list[int]:
        return list(self._info)

    def create(self, flags: int = 0) -> None:
        self.create_calls += 1

    def reboot(self, flags: int = 0) -> None:
        self.reboot_calls += 1


def _patch_lookup(monkeypatch, dom) -> None:
    monkeypatch.setattr(domains, "lookup_domain", lambda conn, name: dom)


# ---- start ---------------------------------------------------------------


def test_start_missing_domain_raises(store, monkeypatch) -> None:
    _patch_lookup(monkeypatch, None)
    with pytest.raises(ProvisionError, match="does not exist"):
        provision.start_instance(store, object(), "ghost")


def test_start_already_running_is_idempotent(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_RUNNING)
    _patch_lookup(monkeypatch, dom)
    provision.start_instance(store, object(), "i1")
    assert dom.create_calls == 0
    assert "already running" in capsys.readouterr().out


def test_start_shutoff_creates_domain(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_SHUTOFF)
    _patch_lookup(monkeypatch, dom)
    provision.start_instance(store, object(), "i1")
    assert dom.create_calls == 1
    assert "started" in capsys.readouterr().out


# ---- stop ----------------------------------------------------------------


def test_stop_already_shutoff_skips_shutdown(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_SHUTOFF)
    _patch_lookup(monkeypatch, dom)
    calls: list[tuple] = []

    def fake_stop(conn, name, timeout, force=False):
        calls.append((name, timeout, force))
        return True

    monkeypatch.setattr(domains, "stop_domain", fake_stop)
    provision.stop_instance(store, object(), "i1")
    assert calls == []
    assert "already shut off" in capsys.readouterr().out


def test_stop_running_uses_shared_shutdown(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_RUNNING)
    _patch_lookup(monkeypatch, dom)
    calls: list[tuple] = []
    monkeypatch.setattr(
        domains,
        "stop_domain",
        lambda conn, name, timeout, force=False: calls.append(
            (name, timeout, force)
        )
        or True,
    )

    provision.stop_instance(store, object(), "i1")
    assert calls == [("i1", store.resolve("i1").shutdown_timeout, False)]
    assert "stopped" in capsys.readouterr().out


def test_stop_force_flag_is_forwarded(store, monkeypatch) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_RUNNING)
    _patch_lookup(monkeypatch, dom)
    calls: list[tuple] = []
    monkeypatch.setattr(
        domains,
        "stop_domain",
        lambda conn, name, timeout, force=False: calls.append(
            (timeout, force)
        )
        or True,
    )

    provision.stop_instance(store, object(), "i1", force=True)
    assert calls == [(store.resolve("i1").shutdown_timeout, True)]


def test_stop_timeout_propagates(store, monkeypatch) -> None:
    _patch_lookup(monkeypatch, _Dom(libvirt.VIR_DOMAIN_RUNNING))

    def fail(*args, **kwargs):
        raise ShutdownTimeout("Instance i1 did not shut down within 5s")

    monkeypatch.setattr(domains, "stop_domain", fail)
    with pytest.raises(ShutdownTimeout):
        provision.stop_instance(store, object(), "i1")


# ---- reboot --------------------------------------------------------------


def test_reboot_running_sends_acpi_reboot(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_RUNNING)
    _patch_lookup(monkeypatch, dom)
    provision.reboot_instance(store, object(), "i1")
    assert dom.reboot_calls == 1
    assert "rebooting" in capsys.readouterr().out


def test_reboot_shutoff_raises(store, monkeypatch) -> None:
    _patch_lookup(monkeypatch, _Dom(libvirt.VIR_DOMAIN_SHUTOFF))
    with pytest.raises(ProvisionError, match="not running"):
        provision.reboot_instance(store, object(), "i1")


# ---- status --------------------------------------------------------------


def test_status_registered_running(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_RUNNING, memory_kib=2048, vcpus=2)
    _patch_lookup(monkeypatch, dom)
    monkeypatch.setattr(network, "find_ipv4", lambda conn, name: "10.0.0.5")

    provision.status_instance(store, object(), "i1")
    out = capsys.readouterr().out
    assert "Name:     i1" in out
    assert "Template: t1" in out
    assert "State:    running" in out
    assert "vCPUs:    2" in out
    assert "Memory:   2 MiB" in out
    assert "IPv4:     10.0.0.5" in out


def test_status_unregistered_template_is_dash(store, monkeypatch, capsys) -> None:
    dom = _Dom(libvirt.VIR_DOMAIN_SHUTOFF)
    _patch_lookup(monkeypatch, dom)

    provision.status_instance(store, object(), "ghost")
    out = capsys.readouterr().out
    assert "Name:     ghost" in out
    assert "Template: -" in out
    assert "State:    shut off" in out
    assert "IPv4:     -" in out


def test_status_missing_domain_raises(store, monkeypatch) -> None:
    _patch_lookup(monkeypatch, None)
    with pytest.raises(ProvisionError, match="does not exist"):
        provision.status_instance(store, object(), "ghost")


def test_status_ip_failure_falls_back_to_dash(
    store, monkeypatch, capsys
) -> None:
    _patch_lookup(monkeypatch, _Dom(libvirt.VIR_DOMAIN_RUNNING))

    def no_ip(conn, name):
        raise NetworkError("no IP")

    monkeypatch.setattr(network, "find_ipv4", no_ip)
    provision.status_instance(store, object(), "i1")
    assert "IPv4:     -" in capsys.readouterr().out


# ---- create 等待开关 ------------------------------------------------------


@contextlib.contextmanager
def _fake_seed(_docs):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cidata.iso"
        path.write_bytes(b"seed")
        yield path


def _patch_create_dependencies(monkeypatch, wait_calls: list[dict]) -> None:
    from kvm_cloud_init import qcow2
    from kvm_cloud_init.provision import create as create_module

    monkeypatch.setattr(create_module.domains, "lookup_domain", lambda conn, n: None)
    monkeypatch.setattr(
        create_module.cloudinit,
        "render_docs",
        lambda store, resolved: {"user-data": "#cloud-config\n"},
    )
    monkeypatch.setattr(create_module.ssh, "ensure_keypair", lambda name: None)
    monkeypatch.setattr(
        create_module.ssh, "read_public_key", lambda name: "ssh-ed25519 AAAA"
    )
    monkeypatch.setattr(
        create_module.cloudinit,
        "inject_ssh_access",
        lambda user_data, key: (user_data, "cliff"),
    )
    monkeypatch.setattr(create_module.client, "get_pool", lambda conn, name, refresh=False: object())
    monkeypatch.setattr(create_module, "pool_target_path", lambda pool: "/pool")
    monkeypatch.setattr(
        create_module.hv_storage, "delete_path", lambda conn, pool, path: None
    )
    monkeypatch.setattr(
        create_module.qcow2,
        "read_info",
        lambda path: qcow2.Qcow2Info(virtual_size=1024, backing_file=None),
    )
    monkeypatch.setattr(
        create_module.seediso,
        "temporary_seed",
        _fake_seed,
    )
    monkeypatch.setattr(
        create_module.hv_storage,
        "upload_file",
        lambda pool, name, path, fmt, capacity: object(),
    )
    monkeypatch.setattr(
        create_module.firmware, "choose_firmware", lambda mode, conn: "bios"
    )
    monkeypatch.setattr(
        create_module.guest, "build_domain_xml", lambda **kwargs: "<domain/>"
    )
    monkeypatch.setattr(
        create_module.guest, "define_and_start", lambda conn, xml: None
    )

    def fake_wait(conn, name, *, verbose=False) -> int:
        wait_calls.append({"name": name, "verbose": verbose})
        return 1

    monkeypatch.setattr(create_module, "wait_cloud_init", fake_wait)


def test_create_no_wait_skips_cloud_init(
    store: ConfigStore, project_dir: Path, monkeypatch
) -> None:
    (project_dir / "images" / "base.qcow2").write_bytes(b"")
    wait_calls: list[dict] = []
    _patch_create_dependencies(monkeypatch, wait_calls)

    assert (
        provision.create_instance(store, object(), "i1", no_wait=True) == 0
    )
    assert wait_calls == []


def test_create_waits_by_default_and_returns_code(
    store: ConfigStore, project_dir: Path, monkeypatch
) -> None:
    (project_dir / "images" / "base.qcow2").write_bytes(b"")
    wait_calls: list[dict] = []
    _patch_create_dependencies(monkeypatch, wait_calls)

    assert provision.create_instance(store, object(), "i1") == 1
    assert wait_calls == [{"name": "i1", "verbose": False}]


def test_create_verbose_is_forwarded(
    store: ConfigStore, project_dir: Path, monkeypatch
) -> None:
    (project_dir / "images" / "base.qcow2").write_bytes(b"")
    wait_calls: list[dict] = []
    _patch_create_dependencies(monkeypatch, wait_calls)

    provision.create_instance(store, object(), "i1", verbose=True)
    assert wait_calls == [{"name": "i1", "verbose": True}]
