"""SSH 连接建立与重试。

实例刚启动时 IP 往往先于 sshd 就绪，连接失败按固定间隔重试到截止时间；
认证失败立即失败，重试不会改变结果。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import paramiko

from ..errors import SshError

#: 单次连接超时（秒）
CONNECT_TIMEOUT = 10
#: 连接失败后的重试间隔（秒）
CONNECT_RETRY_INTERVAL = 2.0
#: 连接失败后的重试总时长（秒）；调用方可用 deadline 覆盖
CONNECT_RETRY_TIMEOUT = 60.0
#: 读写缓冲区
_BUFSIZE = 65536


def _connect(
    instance: str,
    host: str,
    user: str,
    key_path: Path,
    *,
    deadline: float | None = None,
    on_retry: Callable[[int, float], None] | None = None,
) -> paramiko.SSHClient:
    """建立 SSH 连接；主机密钥自动接受（适用于本地临时实例）。

    实例刚启动时 IP 往往先于 sshd 就绪，连接失败按 ``CONNECT_RETRY_INTERVAL``
    重试直到 ``deadline``（``time.monotonic()`` 绝对时间，缺省为当前时间 +
    ``CONNECT_RETRY_TIMEOUT``）；认证失败不重试，重试不会改变结果。
    """
    if deadline is None:
        deadline = time.monotonic() + CONNECT_RETRY_TIMEOUT
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=user,
                key_filename=str(key_path),
                timeout=CONNECT_TIMEOUT,
                banner_timeout=CONNECT_TIMEOUT,
                auth_timeout=CONNECT_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SshError(
                f"Cannot connect to instance {instance} ({user}@{host}): {exc}"
            ) from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            now = time.monotonic()
            if now >= deadline:
                waited = int(now - started)
                suffix = f" after {waited}s" if waited >= 1 else ""
                raise SshError(
                    f"Cannot connect to instance {instance} ({user}@{host})"
                    f"{suffix}: {exc}"
                ) from exc
            delay = min(CONNECT_RETRY_INTERVAL, deadline - now)
            if on_retry is not None:
                on_retry(attempt, delay)
            time.sleep(delay)
        else:
            return client
