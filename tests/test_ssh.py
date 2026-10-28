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
    with pytest.raises(SshError, match="Public key"):
        ssh.read_public_key("ghost")


def test_delete_keypair_removes_both_and_is_idempotent(keys_root: Path) -> None:
    ssh.ensure_keypair("vm1")
    removed = ssh.delete_keypair("vm1")
    assert {p.name for p in removed} == {"vm1", "vm1.pub"}
    assert not ssh.private_key_path("vm1").exists()
    assert ssh.delete_keypair("vm1") == []


# ---- 连接与命令执行（mock paramiko 通道） ----


class _FakeChannel:
    def __init__(
        self,
        out: bytes = b"",
        err: bytes = b"",
        status: int = 0,
        hold_exit: int = 0,
    ) -> None:
        self._out = out
        self._err = err
        self._status = status
        # 前 hold_exit 次 exit_status_ready() 强制返回 False，
        # 用于模拟远端仍在运行、输出缓冲却已排空的间隙。
        self._hold_exit = hold_exit
        self.sent = b""
        self.timeouts: list[object] = []
        self.write_shut_down = False

    def setblocking(self, _flag: int) -> None:
        pass

    def fileno(self) -> int:
        return 99

    def settimeout(self, timeout: object) -> None:
        self.timeouts.append(timeout)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def shutdown_write(self) -> None:
        self.write_shut_down = True

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
        if self._hold_exit > 0:
            self._hold_exit -= 1
            return False
        return not self._out and not self._err

    def recv_exit_status(self) -> int:
        return self._status


class _FakeStream:
    def __init__(self, chan: _FakeChannel) -> None:
        self.channel = chan


class _FakeClient:
    def __init__(self, chan: _FakeChannel) -> None:
        self.chan = chan
        self.command: str | None = None
        self.get_pty: bool | None = None
        self.closed = False

    def exec_command(self, command: str, **kwargs):
        self.command = command
        self.get_pty = kwargs.get("get_pty")
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


def test_run_command_requests_pty_when_both_stdio_are_ttys(
    keys_root: Path, monkeypatch, capfd
) -> None:
    chan = _FakeChannel(out=b"$ ", err=b"", status=0)
    client = _FakeClient(chan)
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)
    monkeypatch.setattr(ssh, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr(ssh.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ssh.sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(ssh.termios, "tcgetattr", lambda _fd: ["attr"])
    monkeypatch.setattr(ssh.termios, "tcsetattr", lambda *args: None)
    monkeypatch.setattr(ssh.tty, "setraw", lambda _fd: None)
    monkeypatch.setattr(ssh.signal, "signal", lambda *args: None)
    monkeypatch.setattr(ssh, "_sync_window_size", lambda _chan: None)
    monkeypatch.setattr(
        ssh.select, "select", lambda r, w, x, t: ([99], [], [])
    )

    rc = ssh.run_command("vm1", "10.0.0.5", "cliff", Path("/k"), "bash")

    assert rc == 0
    assert client.get_pty is True
    assert "$ " in capfd.readouterr().out


def test_run_command_pty_forwards_stdin_until_local_eof(
    keys_root: Path, monkeypatch
) -> None:
    # hold_exit=2：前两轮即使输出排空也保持“远端未退出”，
    # 确保 stdin 转发与本地 EOF 分支都能被执行到。
    chan = _FakeChannel(out=b"$ ", status=0, hold_exit=2)
    client = _FakeClient(chan)
    monkeypatch.setattr(ssh, "_connect", lambda *a, **k: client)
    monkeypatch.setattr(ssh, "_stdio_is_tty", lambda: True)
    monkeypatch.setattr(ssh.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ssh.sys.stdin, "fileno", lambda: 0)
    monkeypatch.setattr(ssh.termios, "tcgetattr", lambda _fd: ["attr"])
    monkeypatch.setattr(ssh.termios, "tcsetattr", lambda *args: None)
    monkeypatch.setattr(ssh.tty, "setraw", lambda _fd: None)
    monkeypatch.setattr(ssh.signal, "signal", lambda *args: None)
    monkeypatch.setattr(ssh, "_sync_window_size", lambda _chan: None)
    select_results = iter(([99, 0], [0]))
    monkeypatch.setattr(
        ssh.select,
        "select",
        lambda r, w, x, t: (next(select_results), [], []),
    )
    inputs = iter((b"echo hi\n", b""))
    monkeypatch.setattr(ssh.os, "read", lambda _fd, _n: next(inputs))

    rc = ssh.run_command("vm1", "10.0.0.5", "cliff", Path("/k"), "bash")

    assert rc == 0
    assert client.get_pty is True
    assert chan.sent == b"echo hi\n"
    assert chan.timeouts == [None, 0.0]
    assert chan.write_shut_down is True


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


# ---- 连接重试（IP 先于 sshd 就绪） ----


class _ScriptedClient:
    """按脚本抛出连接异常或成功的假 SSHClient。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.closed = False

    def set_missing_host_key_policy(self, _policy: object) -> None:
        pass

    def connect(self, **kwargs: object) -> None:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def close(self) -> None:
        self.closed = True


def _patch_clients(monkeypatch, outcomes: list[object]) -> list[_ScriptedClient]:
    created: list[_ScriptedClient] = []

    def factory() -> _ScriptedClient:
        client = _ScriptedClient(outcomes)
        created.append(client)
        return client

    monkeypatch.setattr(ssh.paramiko, "SSHClient", factory)
    return created


def test_connect_retries_until_sshd_accepts(monkeypatch) -> None:
    refused = paramiko.SSHException("Unable to connect to port 22")
    clients = _patch_clients(monkeypatch, [refused, refused, None])
    clocks = iter([0.0, 1.0, 3.0])
    monkeypatch.setattr(ssh.time, "monotonic", lambda: next(clocks))
    sleeps: list[float] = []
    monkeypatch.setattr(ssh.time, "sleep", lambda seconds: sleeps.append(seconds))
    retries: list[tuple[int, float]] = []

    client = ssh._connect(
        "vm1",
        "10.0.0.5",
        "cliff",
        Path("/k"),
        deadline=100.0,
        on_retry=lambda attempt, delay: retries.append((attempt, delay)),
    )

    assert client is clients[-1]
    assert len(clients) == 3
    assert clients[0].closed and clients[1].closed
    assert sleeps == [ssh.CONNECT_RETRY_INTERVAL, ssh.CONNECT_RETRY_INTERVAL]
    assert retries == [(1, ssh.CONNECT_RETRY_INTERVAL), (2, ssh.CONNECT_RETRY_INTERVAL)]


def test_connect_gives_up_after_deadline(monkeypatch) -> None:
    refused = paramiko.SSHException("Unable to connect to port 22")
    clients = _patch_clients(monkeypatch, [refused, refused, refused])
    clocks = iter([0.0, 50.0, 100.0])
    monkeypatch.setattr(ssh.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(ssh.time, "sleep", lambda _seconds: None)

    with pytest.raises(SshError, match=r"Cannot connect to instance vm1 .* after 100s"):
        ssh._connect("vm1", "10.0.0.5", "cliff", Path("/k"), deadline=100.0)

    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_connect_does_not_retry_authentication_failure(monkeypatch) -> None:
    clients = _patch_clients(
        monkeypatch, [paramiko.AuthenticationException("bad key")]
    )
    monkeypatch.setattr(ssh.time, "monotonic", lambda: 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(ssh.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(SshError, match="Cannot connect to instance vm1"):
        ssh._connect("vm1", "10.0.0.5", "cliff", Path("/k"), deadline=100.0)

    assert len(clients) == 1
    assert clients[0].closed
    assert sleeps == []
