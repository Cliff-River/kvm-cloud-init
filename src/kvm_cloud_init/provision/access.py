"""SSH 目标解析与 shell / exec 编排。"""

from __future__ import annotations

from pathlib import Path

import libvirt

from .. import ssh
from ..config import ConfigStore
from ..errors import ProvisionError, SshError
from ..hv import domains, guest, network


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


def shell_instance(store: ConfigStore, conn: libvirt.virConnect, name: str) -> int:
    """打开实例的 SSH 交互式登录会话，返回远端退出码。"""
    host, user, key_path = ssh_target(conn, name)
    return ssh.interactive_shell(name, host, user, key_path)


def exec_instance(
    store: ConfigStore, conn: libvirt.virConnect, name: str, command: list[str]
) -> int:
    """在实例中执行一条命令（-- 之后的所有 token 拼接），返回远端退出码。"""
    host, user, key_path = ssh_target(conn, name)
    return ssh.run_command(name, host, user, key_path, " ".join(command))
