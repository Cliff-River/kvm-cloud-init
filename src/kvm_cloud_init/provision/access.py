"""SSH 目标解析与 shell / exec 编排。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import libvirt

from .. import ssh
from ..config import ConfigStore
from ..errors import NetworkError, ProvisionError, SshError
from ..hv import domains, guest, network
from . import lifecycle

#: 等待实例 SSH 可达的最长时间（秒）与轮询间隔
SSH_READY_TIMEOUT = 300
SSH_POLL_INTERVAL = 2.0


def _ensure_running(
    store: ConfigStore, conn: libvirt.virConnect, name: str
) -> None:
    """实例未运行时自动启动，已运行则直接返回。"""
    dom = domains.lookup_domain(conn, name)
    if dom is None:
        raise ProvisionError(f"Instance {name} does not exist")
    if dom.state(0)[0] == libvirt.VIR_DOMAIN_RUNNING:
        return
    print(f"Instance {name} is not running; starting it ...", flush=True)
    lifecycle.start_instance(store, conn, name)
    print(
        f"Waiting for {name} to become reachable over SSH ...", flush=True
    )


def wait_ssh_target(
    conn: libvirt.virConnect,
    name: str,
    *,
    deadline: float | None = None,
    timeout: float = SSH_READY_TIMEOUT,
    on_wait: Callable[[float], None] | None = None,
) -> tuple[str, str, Path]:
    """轮询解析 SSH 目标，直到 IP 可用或超时。"""
    if deadline is None:
        deadline = time.monotonic() + timeout
    while True:
        try:
            return ssh_target(conn, name)
        except NetworkError as exc:
            if time.monotonic() >= deadline:
                raise SshError(
                    f"Instance {name} did not get an IPv4 address "
                    f"within {timeout}s"
                ) from exc
            if on_wait is not None:
                on_wait(SSH_POLL_INTERVAL)
            else:
                time.sleep(SSH_POLL_INTERVAL)


def ssh_target(
    conn: libvirt.virConnect, name: str
) -> tuple[str, str, Path]:
    """解析 shell / exec 所需的目标：实测 IP、登录用户、私钥路径。"""
    dom = domains.lookup_domain(conn, name)
    if dom is None:
        raise ProvisionError(f"Instance {name} does not exist")
    if dom.state(0)[0] != libvirt.VIR_DOMAIN_RUNNING:
        raise ProvisionError(
            f"Instance {name} is not running; start it before connecting"
        )
    user = guest.domain_ssh_user(dom)
    if not user:
        raise SshError(
            f"Instance {name} has no recorded SSH login user; "
            f"run 'kvmci create <name>' again"
        )
    key_path = ssh.private_key_path(name)
    if not key_path.is_file():
        raise SshError(
            f"Private key for instance {name} does not exist: {key_path}; "
            "run 'kvmci create <name>' first"
        )
    host = network.find_ipv4(conn, name)
    return host, user, key_path


def _wait_note(host: str) -> Callable[[int, float], None]:
    """连接重试首次发生时提示用户正在等待，避免看起来像卡死。"""
    notified = False

    def on_retry(_attempt: int, _delay: float) -> None:
        nonlocal notified
        if not notified:
            notified = True
            print(f"Waiting for SSH on {host} ...", flush=True)

    return on_retry


def shell_instance(store: ConfigStore, conn: libvirt.virConnect, name: str) -> int:
    """打开实例的 SSH 交互式登录会话，已关机时先自动启动，返回远端退出码。"""
    _ensure_running(store, conn, name)
    # IP 就绪与 sshd 就绪共用同一个等待窗口：IP 常常早于 sshd 出现。
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    host, user, key_path = wait_ssh_target(conn, name, deadline=deadline)
    return ssh.interactive_shell(
        name,
        host,
        user,
        key_path,
        deadline=deadline,
        on_retry=_wait_note(host),
    )


def exec_instance(
    store: ConfigStore, conn: libvirt.virConnect, name: str, command: list[str]
) -> int:
    """在实例中执行一条命令（-- 之后的所有 token 拼接），已关机时先自动启动。"""
    _ensure_running(store, conn, name)
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    host, user, key_path = wait_ssh_target(conn, name, deadline=deadline)
    return ssh.run_command(
        name,
        host,
        user,
        key_path,
        " ".join(command),
        deadline=deadline,
        on_retry=_wait_note(host),
    )
