"""基于 paramiko Transport 的本地端口转发（等价 ``ssh -L`` 的简化版）。

每条映射在宿主机 ``127.0.0.1`` 上监听一个本地端口；本地连接到达后，
复用已有的 SSH Transport 打开 ``direct-tcpip`` channel 连接到虚拟机内的
``127.0.0.1:<远端端口>``，再在 socket 与 channel 之间双向搬运字节。
不调用 ``ssh`` 子进程，也不引入第三方隧道库。
"""

from __future__ import annotations

import contextlib
import select
import socket
import socketserver
import sys
import threading
from collections.abc import Sequence

import paramiko

from .errors import SshError

#: 转发搬运缓冲区与 select 超时（秒；仅用于周期性回到循环检查退出）
_BUFSIZE = 65536
_SELECT_TIMEOUT = 1.0

#: 仅监听回环地址：转发端口默认不应对局域网暴露
BIND_HOST = "127.0.0.1"

PortMapping = tuple[int, int]


def parse_port_forwards(text: str) -> list[PortMapping]:
    """解析 ``8080:80,443:443`` 形式的端口映射。

    返回 ``[(本地端口, 虚拟机端口), ...]``；任何格式错误抛 ``ValueError``，
    消息为可直接展示的英文，由 CLI 层转换为参数错误。
    """
    mappings: list[PortMapping] = []
    seen_local: set[int] = set()
    for raw in text.split(","):
        piece = raw.strip()
        if not piece or piece.count(":") != 1:
            raise ValueError(
                f"Invalid port forwarding '{raw.strip()}': expected "
                "'LOCAL_PORT:REMOTE_PORT'"
            )
        local_text, remote_text = piece.split(":")
        try:
            local_port = int(local_text)
            remote_port = int(remote_text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid port forwarding '{piece}': ports must be integers"
            ) from exc
        if not 1 <= local_port <= 65535 or not 1 <= remote_port <= 65535:
            raise ValueError(
                f"Invalid port forwarding '{piece}': ports must be in 1-65535"
            )
        if local_port in seen_local:
            raise ValueError(f"Duplicate local port in port forwarding: {local_port}")
        seen_local.add(local_port)
        mappings.append((local_port, remote_port))
    return mappings


class _ForwardServer(socketserver.ThreadingTCPServer):
    """每个本地端口一个监听服务器，连接处理放到独立守护线程。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, server_address: tuple[str, int], transport: paramiko.Transport,
        remote_port: int,
    ) -> None:
        super().__init__(server_address, _ForwardHandler)
        self.transport = transport
        self.remote_port = remote_port


def _relay(sock: socket.socket, chan: paramiko.Channel) -> None:
    """在本地 socket 与 SSH channel 之间双向转发直到对端都关闭。"""
    pending = [sock, chan]
    while pending:
        try:
            readable, _, _ = select.select(pending, [], [], _SELECT_TIMEOUT)
        except (OSError, ValueError):
            return
        for src in list(readable):
            dst = chan if src is sock else sock
            try:
                data = src.recv(_BUFSIZE)
            except (OSError, paramiko.SSHException):
                data = b""
            if data:
                try:
                    dst.sendall(data)
                except (OSError, paramiko.SSHException):
                    return
                continue
            # 对端 EOF：关闭本侧写方向并停止监听该侧（读侧仍可收到数据）。
            try:
                src.shutdown(socket.SHUT_WR)
            except (OSError, paramiko.SSHException):
                pass
            if src in pending:
                pending.remove(src)


class _ForwardHandler(socketserver.BaseRequestHandler):
    """单个本地连接：打开 direct-tcpip channel 并双向转发。"""

    def handle(self) -> None:
        server: _ForwardServer = self.server  # type: ignore[assignment]
        try:
            chan = server.transport.open_channel(
                "direct-tcpip",
                (BIND_HOST, server.remote_port),
                self.request.getpeername(),
            )
        except (paramiko.SSHException, OSError):
            return
        if chan is None:
            return
        try:
            _relay(self.request, chan)
        finally:
            try:
                chan.close()
            except (OSError, paramiko.SSHException):
                pass


class PortForwarder:
    """管理一组本地监听端口的生命周期。"""

    def __init__(
        self,
        transport: paramiko.Transport,
        mappings: Sequence[PortMapping],
        *,
        bind_host: str = BIND_HOST,
    ) -> None:
        self._transport = transport
        self._mappings = list(mappings)
        self._bind_host = bind_host
        self._servers: list[_ForwardServer] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """绑定并开始服务所有映射；任一本地端口不可用则整体回滚。"""
        for local_port, remote_port in self._mappings:
            try:
                server = _ForwardServer(
                    (self._bind_host, local_port),
                    self._transport,
                    remote_port,
                )
            except OSError as exc:
                self.stop()
                raise SshError(
                    f"Cannot listen on local port {local_port} "
                    f"({self._bind_host}): {exc}"
                ) from exc
            self._servers.append(server)
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"port-forward-{local_port}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
            print(
                f"Forwarding {self._bind_host}:{local_port} -> "
                f"instance 127.0.0.1:{remote_port}",
                file=sys.stderr,
                flush=True,
            )

    def stop(self) -> None:
        """停止 accept 并关闭所有监听 socket；已建立的连接随 channel 关闭。"""
        for server in self._servers:
            with contextlib.suppress(Exception):
                server.shutdown()
            with contextlib.suppress(Exception):
                server.server_close()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._servers.clear()
        self._threads.clear()
