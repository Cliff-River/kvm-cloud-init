"""远端只读原语：捕获命令输出、查询状态、读取日志尾部。"""

from __future__ import annotations

import json
from pathlib import Path

from ... import ssh
from ...errors import CloudInitError
from .constants import STATUS_JSON_CMD


def capture(
    name: str, target: tuple[str, str, Path], command: str
) -> tuple[str, str, int]:
    host, user, key_path = target
    return ssh.capture_command(name, host, user, key_path, command)


def capture_with_sudo(
    name: str, target: tuple[str, str, Path], command: str
) -> tuple[str, str, int]:
    """状态命令失败时回退到免密 sudo，保留有效的非零状态输出。"""
    direct = capture(name, target, command)
    if direct[2] == 0:
        return direct
    elevated = capture(name, target, f"sudo -n {command}")
    if elevated[2] == 0 or not direct[0].strip():
        return elevated
    return direct


def query_state(name: str, target: tuple[str, str, Path]) -> str:
    """读取 ``cloud-init status --format=json`` 的 status 字段。"""
    out, err, code = capture_with_sudo(name, target, STATUS_JSON_CMD)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        if code != 0:
            raise CloudInitError(
                f"Cannot query cloud-init status on instance {name} as user "
                f"'{target[1]}': direct and sudo queries failed. Cloud-init "
                "may already have completed, but permissions or another "
                "command error prevented confirmation. Log in to instance "
                f"{name} and run 'sudo cloud-init status --long' (or use a "
                "root shell) to check it manually."
            ) from exc
        detail = err.strip() or out.strip() or "empty output"
        raise CloudInitError(
            f"Cannot parse 'cloud-init status' output: {detail}"
        ) from exc
    state = data.get("status")
    if not isinstance(state, str):
        raise CloudInitError(
            f"'cloud-init status' output has no string status field: {out.strip()}"
        )
    return state


def tail_lines(
    name: str, target: tuple[str, str, Path], path: str, count: int
) -> list[str]:
    """读取文件末尾若干行；普通用户无权限时回退到 ``sudo -n``。"""
    command = f"tail -n {count} {path}"
    out, err, code = capture(name, target, command)
    if code != 0:
        out, err, code = capture(name, target, f"sudo -n {command}")
    if code != 0:
        detail = err.strip() or "permission denied"
        raise CloudInitError(f"Cannot read {path}: {detail}")
    return out.rstrip("\n").splitlines()


def can_read(
    name: str, target: tuple[str, str, Path], path: str, *, sudo: bool = False
) -> bool:
    prefix = "sudo -n " if sudo else ""
    _out, _err, code = capture(name, target, f"{prefix}test -r {path}")
    return code == 0
