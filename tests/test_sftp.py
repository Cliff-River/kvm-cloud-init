"""SFTP 上传/下载测试：递归目录、符号链接与错误原因分类（不建立真实连接）。

用一个由本地临时目录支撑的 FakeSFTP 模拟远端文件系统，
paramiko.SFTPAttributes 直接由 ``os.lstat`` 构造。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import paramiko
import pytest

from kvm_cloud_init import ssh
from kvm_cloud_init.errors import SshError


class FakeSFTP:
    """远端文件系统 = ``root`` 本地目录；POSIX 路径按相对根解析。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.closed = False

    def _p(self, remote: str) -> Path:
        # 与真实 SFTP 一致：相对路径（不以 / 开头）相对于默认目录解析，
        # FakeSFTP 的默认（家）目录即 root。
        return self.root / remote.lstrip("/")

    def stat(self, remote: str) -> paramiko.SFTPAttributes:
        path = self._p(remote)
        if not path.is_symlink() and not path.exists():
            raise FileNotFoundError(2, "No such file")
        return paramiko.SFTPAttributes.from_stat(path.stat(), path.name)

    def lstat(self, remote: str) -> paramiko.SFTPAttributes:
        path = self._p(remote)
        if not path.is_symlink() and not path.exists():
            raise FileNotFoundError(2, "No such file")
        return paramiko.SFTPAttributes.from_stat(os.lstat(path), path.name)

    def listdir_attr(self, remote: str) -> list[paramiko.SFTPAttributes]:
        path = self._p(remote)
        if not path.is_dir():
            raise FileNotFoundError(2, "No such file")
        return [
            paramiko.SFTPAttributes.from_stat(os.lstat(child), child.name)
            for child in sorted(path.iterdir(), key=lambda item: item.name)
        ]

    def normalize(self, remote: str) -> str:
        """模拟服务端 realpath：解析 ``.``/``..`` 并返回 root 内的绝对路径。"""
        resolved = self._p(remote).resolve()
        try:
            rel = resolved.relative_to(self.root.resolve())
        except ValueError:
            return str(resolved)
        return "/" if str(rel) == "." else "/" + rel.as_posix()

    def mkdir(self, remote: str) -> None:
        self._p(remote).mkdir()

    def put(self, local: str, remote: str) -> None:
        shutil.copyfile(local, self._p(remote))

    def get(self, remote: str, local: str) -> None:
        shutil.copyfile(self._p(remote), local)

    def readlink(self, remote: str) -> str:
        return os.readlink(self._p(remote))

    def symlink(self, target: str, remote: str) -> None:
        os.symlink(target, self._p(remote))

    def remove(self, remote: str) -> None:
        self._p(remote).unlink()

    def rmdir(self, remote: str) -> None:
        self._p(remote).rmdir()

    def utime(self, remote: str, times: tuple[float, float]) -> None:
        os.utime(self._p(remote), times)

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, sftp: FakeSFTP) -> None:
        self._sftp = sftp
        self.closed = False

    def open_sftp(self) -> FakeSFTP:
        return self._sftp

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def remote_tree(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """:returns: (远端根目录, 本地下载目标目录)，并把 _connect 钉到 FakeClient。"""
    root = tmp_path / "remote"
    root.mkdir()
    dest = tmp_path / "local"
    dest.mkdir()

    def fake_connect(instance, host, user, key_path, *, deadline=None, on_retry=None):
        return FakeClient(FakeSFTP(root))

    monkeypatch.setattr(ssh.client, "_connect", fake_connect)
    return root, dest


def _build_tree(root: Path) -> None:
    (root / "kube").mkdir()
    (root / "kube" / "a.txt").write_text("a")
    (root / "kube" / "sub").mkdir()
    (root / "kube" / "sub" / "b.txt").write_text("b")
    os.symlink("a.txt", root / "kube" / "link")


def test_download_file(remote_tree) -> None:
    root, dest = remote_tree
    (root / "app.conf").write_text("config")
    target = dest / "app.conf"

    count = ssh.download_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), "/app.conf", target
    )

    assert count == 1
    assert target.read_text() == "config"


def test_download_directory_recursively_with_symlink(remote_tree) -> None:
    root, dest = remote_tree
    _build_tree(root)

    count = ssh.download_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), "/kube", dest / "kube"
    )

    assert count == 2
    assert (dest / "kube" / "a.txt").read_text() == "a"
    assert (dest / "kube" / "sub" / "b.txt").read_text() == "b"
    link = dest / "kube" / "link"
    assert link.is_symlink()
    assert os.readlink(link) == "a.txt"


def test_download_into_existing_directory_merges(remote_tree) -> None:
    root, dest = remote_tree
    _build_tree(root)
    # dest 本身作为目标父目录：kvmci 在目标已是目录时会传入 dest/kube
    count = ssh.download_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), "/kube", dest / "kube"
    )
    assert count == 2
    # 再次下载应合并而非报错
    count = ssh.download_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), "/kube", dest / "kube"
    )
    assert count == 2


def test_download_missing_remote_reports_specific_reason(remote_tree) -> None:
    _root, dest = remote_tree
    with pytest.raises(SshError, match="Remote path not found: vm1:/nope"):
        ssh.download_path(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/nope", dest / "nope",
        )


def test_download_to_missing_local_parent_reports_specific_reason(remote_tree) -> None:
    root, dest = remote_tree
    (root / "a.txt").write_text("x")
    with pytest.raises(SshError, match="Local directory does not exist"):
        ssh.download_path(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/a.txt", dest / "nodir" / "a.txt",
        )


def test_download_generic_failure_is_translated(remote_tree, monkeypatch) -> None:
    root, dest = remote_tree
    (root / "a.txt").write_text("x")

    def boom(self, remote: str) -> paramiko.SFTPAttributes:
        raise OSError("Failure")

    monkeypatch.setattr(FakeSFTP, "lstat", boom)
    with pytest.raises(SshError, match="SFTP server rejected the operation"):
        ssh.download_path(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/a.txt", dest / "a.txt",
        )


def test_download_permission_error_is_specific(remote_tree, monkeypatch) -> None:
    root, dest = remote_tree
    (root / "a.txt").write_text("x")

    def boom(self, remote: str) -> paramiko.SFTPAttributes:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(FakeSFTP, "lstat", boom)
    with pytest.raises(SshError, match="permission denied"):
        ssh.download_path(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/a.txt", dest / "a.txt",
        )


def test_upload_file_creates_remote_parents(remote_tree, tmp_path: Path) -> None:
    root, _dest = remote_tree
    local = tmp_path / "app.conf"
    local.write_text("config")

    count = ssh.upload_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
        local, "/etc/app/app.conf",
    )

    assert count == 1
    assert (root / "etc" / "app" / "app.conf").read_text() == "config"


def test_upload_directory_recursively_with_symlink(remote_tree, tmp_path: Path) -> None:
    root, _dest = remote_tree
    local = tmp_path / "kube"
    local.mkdir()
    (local / "a.txt").write_text("a")
    (local / "sub").mkdir()
    (local / "sub" / "b.txt").write_text("b")
    os.symlink("a.txt", local / "link")

    count = ssh.upload_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), local, "/srv/kube"
    )

    assert count == 2
    assert (root / "srv" / "kube" / "a.txt").read_text() == "a"
    assert (root / "srv" / "kube" / "sub" / "b.txt").read_text() == "b"
    link = root / "srv" / "kube" / "link"
    assert link.is_symlink()
    assert os.readlink(link) == "a.txt"


def test_upload_missing_local_path_raises_before_connecting(
    remote_tree, tmp_path: Path
) -> None:
    with pytest.raises(SshError, match="Local path not found"):
        ssh.upload_path(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            tmp_path / "missing", "/x",
        )


def test_sftp_session_is_closed(remote_tree, tmp_path: Path, monkeypatch) -> None:
    root, _dest = remote_tree
    local = tmp_path / "a.txt"
    local.write_text("x")
    client = FakeClient(FakeSFTP(root))
    monkeypatch.setattr(ssh.client, "_connect", lambda *a, **k: client)

    ssh.upload_path(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), local, "/a.txt"
    )
    assert client.closed is True
    assert client._sftp.closed is True
