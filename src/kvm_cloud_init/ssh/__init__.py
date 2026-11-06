"""SSH 传输能力包：密钥、连接、会话、命令、SFTP 与端口转发。

原 ``ssh.py`` 的门面；所有公共符号保持 ``kvm_cloud_init.ssh.*`` 路径不变。
包内子模块之间通过模块对象调用（``from . import x`` + ``x.func``），
以便测试替换底层实现时能影响全部调用点。
"""

from __future__ import annotations

from ..errors import SshError
from .client import (
    CONNECT_RETRY_INTERVAL,
    CONNECT_RETRY_TIMEOUT,
    CONNECT_TIMEOUT,
)
from .command import capture_command, stream_command
from .keys import (
    delete_keypair,
    ensure_keypair,
    keys_dir,
    private_key_path,
    public_key_path,
    read_public_key,
)
from .portforward import (
    BIND_HOST,
    PortForwarder,
    PortMapping,
    parse_port_forwards,
)
from .session import interactive_shell, run_command
from .sftp import download_path, remote_is_directory, upload_path
from .sync import SyncStats, sync_from_remote, sync_to_remote

__all__ = [
    "BIND_HOST",
    "CONNECT_RETRY_INTERVAL",
    "CONNECT_RETRY_TIMEOUT",
    "CONNECT_TIMEOUT",
    "PortForwarder",
    "PortMapping",
    "SshError",
    "SyncStats",
    "capture_command",
    "delete_keypair",
    "download_path",
    "ensure_keypair",
    "interactive_shell",
    "keys_dir",
    "parse_port_forwards",
    "private_key_path",
    "public_key_path",
    "read_public_key",
    "remote_is_directory",
    "run_command",
    "stream_command",
    "sync_from_remote",
    "sync_to_remote",
    "upload_path",
]
