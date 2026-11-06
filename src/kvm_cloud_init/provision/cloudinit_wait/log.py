"""cloud-init 状态查询与日志输出。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import libvirt

from ... import ssh
from .. import access
from . import query
from .constants import LOG_FILE, POLL_INTERVAL, STATUS_LONG_CMD


def cloud_init_status(
    conn: libvirt.virConnect, name: str
) -> int:
    """打印 cloud-init 状态；done=0，running/not started=2，error=1。"""
    target = access.ssh_target(conn, name)
    state = query._query_state(name, target)

    out, err, _code = query._capture(name, target, STATUS_LONG_CMD)
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip(), file=sys.stderr)

    if state == "done":
        return 0
    if state == "error":
        return 1
    return 2


def cloud_init_log(
    conn: libvirt.virConnect, name: str, *, follow: bool = False
) -> int:
    """输出 cloud-init 日志；``follow`` 时实时跟随到 cloud-init 结束。"""
    target = access.ssh_target(conn, name)

    if not follow:
        return _print_log(name, target)

    state = query._query_state(name, target)
    if state in ("done", "error"):
        _print_log(name, target)
        return 0 if state == "done" else 1

    holder = {"state": state, "checked": 0.0}

    def should_stop() -> bool:
        now = time.monotonic()
        if now - holder["checked"] >= POLL_INTERVAL:
            try:
                holder["state"] = query._query_state(name, target)
            except ssh.SshError:
                pass
            holder["checked"] = now
        return holder["state"] in ("done", "error")

    host, user, key_path = target
    command = f"tail -n +1 -F {LOG_FILE}"
    if query._can_read(name, target, LOG_FILE):
        pass
    elif query._can_read(name, target, LOG_FILE, sudo=True):
        command = f"sudo -n {command}"
    else:
        print(
            f"Cannot read {LOG_FILE}: permission denied "
            "(passwordless sudo is required)",
            file=sys.stderr,
        )
        return 1

    ssh.stream_command(
        name,
        host,
        user,
        key_path,
        command,
        should_stop=should_stop,
    )
    return 0 if holder["state"] == "done" else 1


def _print_log(name: str, target: tuple[str, str, Path]) -> int:
    out, err, code = query._capture(name, target, f"cat {LOG_FILE}")
    if code != 0:
        out, err, code = query._capture(name, target, f"sudo -n cat {LOG_FILE}")
    sys.stdout.write(out)
    sys.stdout.flush()
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return code
