"""远端只读原语：捕获命令输出、查询状态、读取日志尾部。"""

from __future__ import annotations

import json
from pathlib import Path

from ... import ssh
from ...errors import CloudInitError
from .constants import STATUS_JSON_CMD


def _capture(
    name: str, target: tuple[str, str, Path], command: str
) -> tuple[str, str, int]:
    host, user, key_path = target
    return ssh.capture_command(name, host, user, key_path, command)


def _query_state(name: str, target: tuple[str, str, Path]) -> str:
    """读取 ``cloud-init status --format=json`` 的 status 字段。"""
    out, err, _code = _capture(name, target, STATUS_JSON_CMD)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
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


def _tail_lines(
    name: str, target: tuple[str, str, Path], path: str, count: int
) -> list[str]:
    """读取文件末尾若干行；普通用户无权限时回退到 ``sudo -n``。"""
    command = f"tail -n {count} {path}"
    out, err, code = _capture(name, target, command)
    if code != 0:
        out, err, code = _capture(name, target, f"sudo -n {command}")
    if code != 0:
        detail = err.strip() or "permission denied"
        raise CloudInitError(f"Cannot read {path}: {detail}")
    return out.rstrip("\n").splitlines()


def _can_read(
    name: str, target: tuple[str, str, Path], path: str, *, sudo: bool = False
) -> bool:
    prefix = "sudo -n " if sudo else ""
    _out, _err, code = _capture(name, target, f"{prefix}test -r {path}")
    return code == 0
