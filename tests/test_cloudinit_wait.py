"""cloud-init 等待、状态与日志测试（全部 mock，不做真实 SSH）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kvm_cloud_init.errors import CloudInitError
from kvm_cloud_init.provision import cloudinit_wait

TARGET = ("10.0.0.5", "cliff", Path("/keys/vm"))


@pytest.fixture(autouse=True)
def _patch_target(monkeypatch):
    monkeypatch.setattr(
        cloudinit_wait.access, "ssh_target", lambda conn, name: TARGET
    )
    monkeypatch.setattr(cloudinit_wait.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(cloudinit_wait.time, "sleep", lambda _s: None)


def _capture_key(command: str) -> str:
    if command == cloudinit_wait.STATUS_JSON_CMD:
        return "json"
    if command == cloudinit_wait.STATUS_LONG_CMD:
        return "long"
    return "tail"


def _patch_capture(monkeypatch, json_status: str, tail: str = "") -> None:
    def fake_capture(instance, host, user, key_path, command, *, timeout=None):
        key = _capture_key(command)
        if key == "json":
            return f'{{"status": "{json_status}"}}', "", 0
        if key == "long":
            return f"Cloud-init status: {json_status}", "", 0
        return tail, "", 0

    monkeypatch.setattr(cloudinit_wait.ssh, "capture_command", fake_capture)


# ---- wait_cloud_init -----------------------------------------------------


def test_wait_success_returns_zero(monkeypatch, capsys) -> None:
    _patch_capture(monkeypatch, "done")
    assert cloudinit_wait.wait_cloud_init(object(), "vm") == 0
    assert "Cloud-init completed successfully" in capsys.readouterr().out


def test_wait_error_prints_digest_and_returns_one(monkeypatch, capsys) -> None:
    _patch_capture(monkeypatch, "error", tail="boom\nTraceback: bad")

    assert cloudinit_wait.wait_cloud_init(object(), "vm") == 1
    captured = capsys.readouterr()
    assert "Cloud-init failed:" in captured.err
    assert "Cloud-init status: error" in captured.err
    assert "Traceback: bad" in captured.err


def test_wait_ctrl_c_leaves_instance_running(monkeypatch, capsys) -> None:
    _patch_capture(monkeypatch, "running")

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cloudinit_wait.time, "sleep", interrupt)

    assert cloudinit_wait.wait_cloud_init(object(), "vm") == 0
    assert (
        "Stopped waiting for cloud-init. Instance vm keeps running."
        in capsys.readouterr().out
    )


def test_wait_verbose_non_tty_appends_without_ansi(monkeypatch, capsys) -> None:
    _patch_capture(monkeypatch, "done", tail="line-a\nline-b")

    assert cloudinit_wait.wait_cloud_init(object(), "vm", verbose=True) == 0
    out = capsys.readouterr().out
    assert "line-a" in out
    assert "line-b" in out
    assert "\x1b[" not in out


def test_wait_ssh_timeout_raises_cloudinit_error(monkeypatch) -> None:
    from kvm_cloud_init.errors import NetworkError

    def no_target(conn, name):
        raise NetworkError("no IP")

    monkeypatch.setattr(cloudinit_wait.access, "ssh_target", no_target)

    clock = iter([100.0, 100.0, 100.0, 401.0])
    monkeypatch.setattr(cloudinit_wait.time, "monotonic", lambda: next(clock))
    with pytest.raises(CloudInitError, match="did not become reachable"):
        cloudinit_wait.wait_cloud_init(object(), "vm")


def test_wait_bad_json_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        cloudinit_wait.ssh,
        "capture_command",
        lambda *args, **kwargs: ("not json", "", 0),
    )
    with pytest.raises(CloudInitError, match="Cannot parse"):
        cloudinit_wait.wait_cloud_init(object(), "vm")


# ---- cloud-init status ---------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [("done", 0), ("running", 2), ("not started", 2), ("error", 1)],
)
def test_cloud_init_status_exit_codes(
    monkeypatch, capsys, state: str, expected: int
) -> None:
    _patch_capture(monkeypatch, state)
    assert cloudinit_wait.cloud_init_status(object(), "vm") == expected
    assert f"Cloud-init status: {state}" in capsys.readouterr().out


# ---- cloud-init log ------------------------------------------------------


def test_cloud_init_log_prints_full_log(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cloudinit_wait.ssh,
        "capture_command",
        lambda *args, **kwargs: ("full log\n", "", 0),
    )
    assert cloudinit_wait.cloud_init_log(object(), "vm") == 0
    assert "full log" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("state", "expected"), [("done", 0), ("error", 1)]
)
def test_cloud_init_log_follow_finished_does_not_hang(
    monkeypatch, capsys, state: str, expected: int
) -> None:
    _patch_capture(monkeypatch, state)
    calls: list[str] = []
    monkeypatch.setattr(
        cloudinit_wait.ssh,
        "stream_command",
        lambda *args, **kwargs: calls.append("stream") or 0,
    )

    assert cloudinit_wait.cloud_init_log(object(), "vm", follow=True) == expected
    assert calls == []


def test_cloud_init_log_follow_streams_until_done(monkeypatch) -> None:
    statuses = iter(["running", "done"])

    def capture(instance, host, user, key_path, command, *, timeout=None):
        if command == cloudinit_wait.STATUS_JSON_CMD:
            return f'{{"status": "{next(statuses)}"}}', "", 0
        return "log\n", "", 0

    monkeypatch.setattr(cloudinit_wait.ssh, "capture_command", capture)
    clocks = iter([10.0, 20.0, 30.0])
    monkeypatch.setattr(
        cloudinit_wait.time, "monotonic", lambda: next(clocks)
    )

    seen: dict[str, object] = {}

    def fake_stream(
        instance, host, user, key_path, command, *, should_stop=None
    ) -> int:
        seen["command"] = command
        seen["stopped"] = should_stop() if should_stop is not None else None
        return 0

    monkeypatch.setattr(cloudinit_wait.ssh, "stream_command", fake_stream)

    assert cloudinit_wait.cloud_init_log(object(), "vm", follow=True) == 0
    assert seen["command"] == f"tail -n +1 -F {cloudinit_wait.LOG_FILE}"
    assert seen["stopped"] is True
