"""SSH 密钥管理与连接测试（全部 mock，不建立真实连接）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import paramiko
import pytest

from kvm_cloud_init import ssh
from kvm_cloud_init.errors import SshError


@pytest.fixture
def keys_root(tmp_path: Path, monkeypatch) -> Path:
    """把密钥目录重定向到临时目录，避免触碰仓库真实 keys/。"""
    monkeypatch.setattr(ssh, "project_root", lambda: tmp_path)
    return tmp_path


# ---- 密钥生成 / 复用 / 删除 ----


def test_ensure_keypair_creates_with_permissions(keys_root: Path) -> None:
    priv = ssh.ensure_keypair("vm1")
    pub = ssh.public_key_path("vm1")
    assert priv.is_file() and pub.is_file()
    assert stat.S_IMODE(os.stat(priv).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(pub).st_mode) == 0o644
    assert ssh.read_public_key("vm1").split()[0] in {"ssh-rsa", "ssh-ed25519"}


def test_ensure_keypair_is_idempotent(keys_root: Path) -> None:
    priv = ssh.ensure_keypair("vm1")
    before = priv.read_bytes()
    ssh.ensure_keypair("vm1")
    assert priv.read_bytes() == before


def test_generated_private_key_is_loadable(keys_root: Path) -> None:
    priv = ssh.ensure_keypair("vm1")
    line = ssh.read_public_key("vm1")
    key_type = line.split()[0]
    loaded = (
        paramiko.Ed25519Key.from_private_key_file(str(priv))
        if key_type == "ssh-ed25519"
        else paramiko.RSAKey.from_private_key_file(str(priv))
    )
    assert loaded.get_name() == key_type


def test_read_public_key_missing(keys_root: Path) -> None:
    with pytest.raises(SshError, match="公钥不存在"):
        ssh.read_public_key("ghost")


def test_delete_keypair_removes_both_and_is_idempotent(keys_root: Path) -> None:
    ssh.ensure_keypair("vm1")
    removed = ssh.delete_keypair("vm1")
    assert {p.name for p in removed} == {"vm1", "vm1.pub"}
    assert not ssh.private_key_path("vm1").exists()
    assert ssh.delete_keypair("vm1") == []


# ---- 连接与命令执行（mock paramiko 通道） ----


class _FakeChannel:
    def __init__(self, out: bytes = b"", err: bytes = b"", status: int = 0) -> None:
        self._out = out
        self._err = err
        self._status = status

    def setblocking(self, _flag: int) -> None:
        pass

    def recv_ready(self) -> bool:
        return bool(self._out)

    def recv(self, size: int) -> bytes:
        chunk, self._out = self._out[:size], self._out[size:]
        return chunk

    def recv_stderr_ready(self) -> bool:
        return bool(self._err)

    def recv_stderr(self, size: int) -> bytes:
        chunk, self._err = self._err[:size], self._err[size:]
        return chunk

    def exit_status_ready(self) -> bool:
        return not self._out and not self._err

    def recv_exit_status(self) -> int:
        return self._status


class _FakeStream:
    def __init__(self, chan: _FakeChannel) -> None:
        self.channel = chan

    def read(self) -> bytes:
        out = b""
        while self.channel.recv_ready():
            out += self.channel.recv(65536)
        return out


class _FakeClient:
    def __init__(self, chan: _FakeChannel) -> None:
        self.chan = chan
        self.command: str | None = None
        self.closed = False

    def exec_command(self, command: str, timeout: float | None = None):
        self.command = command
        return None, _FakeStream(self.chan), _FakeStream(self.chan)

    def invoke_shell(self, *args, **kwargs):
        return self.chan

    def close(self) -> None:
        self.closed = True


def test_run_command_passes_command_and_returns_remote_status(
    keys_root: Path, monkeypatch, capfd
) -> None:
    chan = _FakeChannel(out=b"hello\n", err=b"", status=3)
    client = _FakeClient(chan)
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)

    rc = ssh.run_command("vm1", "10.0.0.5", "cliff", Path("/k"), "ls -l /")

    assert rc == 3
    assert client.command == "ls -l /"
    assert client.closed is True
    assert "hello" in capfd.readouterr().out


def test_run_capture_returns_status_and_output(keys_root: Path, monkeypatch) -> None:
    chan = _FakeChannel(out=b"status: done\n", status=0)
    client = _FakeClient(chan)
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)

    rc, out = ssh.run_capture(
        "vm1", "10.0.0.5", "cliff", Path("/k"), "cloud-init status"
    )

    assert (rc, out) == (0, "status: done\n")
    assert client.command == "cloud-init status"
    assert client.closed is True


def test_run_capture_wraps_failures(keys_root: Path, monkeypatch) -> None:
    client = _FakeClient(_FakeChannel())
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)

    def explode() -> bytes:
        raise OSError("通道已关闭")

    monkeypatch.setattr(_FakeStream, "read", lambda self: explode())

    with pytest.raises(SshError, match="执行命令失败"):
        ssh.run_capture("vm1", "10.0.0.5", "cliff", Path("/k"), "cloud-init status")
    assert client.closed is True


def test_interactive_shell_returns_remote_exit_status(
    keys_root: Path, monkeypatch, capfd
) -> None:
    chan = _FakeChannel(out=b"hi\n", err=b"", status=5)
    client = _FakeClient(chan)
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)
    monkeypatch.setattr(ssh, "_sync_window_size", lambda c: None)
    monkeypatch.setattr(ssh.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ssh.sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(
        ssh.select, "select", lambda r, w, x, t: ([chan], [], [])
    )

    rc = ssh.interactive_shell("vm1", "10.0.0.5", "cliff", Path("/k"))

    assert rc == 5
    assert "hi" in capfd.readouterr().out
    assert client.closed is True
