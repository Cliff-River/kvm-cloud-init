"""端口转发解析与本地转发服务器测试（仅使用本机 socket，无真实 SSH）。"""

from __future__ import annotations

import socket
import socketserver
import threading

import pytest

from kvm_cloud_init.errors import SshError
from kvm_cloud_init.ssh.portforward import (
    PortForwarder,
    _ForwardServer,
    parse_port_forwards,
)

# ---- 参数解析 ----


def test_parse_single_mapping() -> None:
    assert parse_port_forwards("8080:80") == [(8080, 80)]


def test_parse_multiple_mappings_with_whitespace() -> None:
    assert parse_port_forwards("8080:80, 443:443") == [(8080, 80), (443, 443)]


@pytest.mark.parametrize(
    "text",
    [
        "8080",
        "8080:80:80",
        "abc:80",
        "80:abc",
        "0:80",
        "70000:80",
        "8080:0",
        "8080:65536",
        "8080:80,8080:81",
        ",80:80",
    ],
)
def test_parse_rejects_invalid_mappings(text: str) -> None:
    with pytest.raises(ValueError):
        parse_port_forwards(text)


# ---- 转发服务器生命周期 ----


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class _FakeTransport:
    """open_channel 时连接到本机 echo 服务，用真实 socket 充当 channel。"""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.calls: list[tuple[str, tuple[str, int], tuple[str, int]]] = []

    def open_channel(self, kind: str, dest_addr, src_addr):
        self.calls.append((kind, dest_addr, src_addr))
        assert kind == "direct-tcpip"
        assert dest_addr == ("127.0.0.1", self.target_port)
        return socket.create_connection(("127.0.0.1", self.target_port), timeout=2)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            data = self.request.recv(4096)
            if not data:
                break
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def test_forwarder_relays_local_connection_to_channel() -> None:
    echo = _EchoServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=echo.serve_forever, daemon=True)
    thread.start()
    try:
        transport = _FakeTransport(echo.server_address[1])
        local_port = _free_port()
        forwarder = PortForwarder(transport, [(local_port, echo.server_address[1])])
        forwarder.start()
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=2) as client:
                client.sendall(b"ping")
                assert client.recv(4) == b"ping"
            assert transport.calls[0][0] == "direct-tcpip"
        finally:
            forwarder.stop()

        # stop 后监听端口必须释放
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", local_port), timeout=0.5)
    finally:
        echo.shutdown()
        echo.server_close()


def test_forwarder_raises_when_local_port_in_use() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        forwarder = PortForwarder(
            _FakeTransport(80), [(blocker.getsockname()[1], 80)]
        )
        with pytest.raises(SshError, match="Cannot listen on local port"):
            forwarder.start()
    finally:
        blocker.close()


def test_forwarder_server_class_is_threaded() -> None:
    assert _ForwardServer.daemon_threads is True
    assert _ForwardServer.allow_reuse_address is True
