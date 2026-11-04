"""sync（rclone sync 语义）测试：增量传输、镜像删除、类型替换与符号链接。

复用 test_sftp.py 中由本地目录支撑的 FakeSFTP/FakeClient，全部无真实连接。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_sftp import FakeClient, FakeSFTP  # type: ignore[import-not-found]

from kvm_cloud_init import ssh
from kvm_cloud_init.errors import ProvisionError, SshError
from kvm_cloud_init.provision import sync as syncmod

#: 固定的源文件 mtime（秒），保证“再同步一次”时 size+mtime 判定确定性
FIXED_MTIME = 1_700_000_000


@pytest.fixture
def roots(tmp_path: Path, monkeypatch):
    """:returns: (本地源/目标根, 远端根)，_connect 钉到 FakeClient。"""
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()

    def fake_connect(instance, host, user, key_path, *, deadline=None, on_retry=None):
        return FakeClient(FakeSFTP(remote))

    monkeypatch.setattr(ssh, "_connect", fake_connect)
    return local, remote


def _set_mtime(path: Path, value: int = FIXED_MTIME) -> None:
    os.utime(path, (value, value))


def _build_source(src: Path) -> None:
    (src / "sub").mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("bb")
    os.symlink("a.txt", src / "link")
    _set_mtime(src / "a.txt")
    _set_mtime(src / "sub" / "b.txt")


# ---- 本地 -> 远端 ----


def test_sync_upload_creates_full_mirror(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    _build_source(src)

    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/srv/site"
    )

    dst = remote / "srv" / "site"
    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "sub" / "b.txt").read_text() == "bb"
    link = dst / "link"
    assert link.is_symlink() and os.readlink(link) == "a.txt"
    assert stats.files_transferred == 2
    assert stats.dirs_created >= 2  # /srv/site 自身 + sub
    assert stats.symlinks_created == 1
    assert stats.deleted_entries == 0
    # mtime 已保留到远端
    assert int((dst / "a.txt").stat().st_mtime) == FIXED_MTIME


def test_sync_upload_second_run_is_noop(roots) -> None:
    local, _remote = roots
    src = local / "site"
    src.mkdir()
    _build_source(src)
    kwargs = {
        "instance": "vm1", "host": "10.0.0.5", "user": "cliff",
        "key_path": Path("/k/vm1"), "remote_path": "/srv/site",
    }
    ssh.sync_to_remote(**kwargs, local_dir=src)

    stats = ssh.sync_to_remote(**kwargs, local_dir=src)

    assert stats.files_transferred == 0
    assert stats.dirs_created == 0
    assert stats.symlinks_created == 0
    assert stats.deleted_entries == 0


def test_sync_upload_transfers_changed_file_and_deletes_missing(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    _build_source(src)
    kwargs = {
        "instance": "vm1", "host": "10.0.0.5", "user": "cliff",
        "key_path": Path("/k/vm1"), "remote_path": "/srv/site",
    }
    ssh.sync_to_remote(**kwargs, local_dir=src)

    # 源中删除 a.txt、修改 b.txt 内容（保持 mtime 不同）
    (src / "a.txt").unlink()
    (src / "link").unlink()
    (src / "sub" / "b.txt").write_text("bbbb")
    _set_mtime(src / "sub" / "b.txt", FIXED_MTIME + 100)

    stats = ssh.sync_to_remote(**kwargs, local_dir=src)

    dst = remote / "srv" / "site"
    assert not (dst / "a.txt").exists()
    assert not (dst / "link").exists()
    assert (dst / "sub" / "b.txt").read_text() == "bbbb"
    assert stats.files_transferred == 1
    assert stats.deleted_entries == 2  # a.txt + link
    assert any(p.endswith("a.txt") for p in stats.deleted)


def test_sync_upload_removes_extra_remote_directory_tree(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    (src / "keep.txt").write_text("k")
    _set_mtime(src / "keep.txt")

    dst = remote / "srv" / "site"
    (dst / "extra" / "deep").mkdir(parents=True)
    (dst / "extra" / "deep" / "x").write_text("x")
    (dst / "stale.txt").write_text("s")

    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/srv/site"
    )

    assert not (dst / "extra").exists()
    assert not (dst / "stale.txt").exists()
    assert (dst / "keep.txt").exists()
    # deep/x、deep、extra 目录与 stale.txt 共 4 个删除条目
    assert stats.deleted_entries == 4


def test_sync_upload_replaces_type_mismatches(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    (src / "was_file").mkdir()
    (src / "was_dir.txt").write_text("now a file")
    _set_mtime(src / "was_dir.txt")

    dst = remote / "srv" / "site"
    dst.mkdir(parents=True)
    (dst / "was_file").write_text("used to be a file")
    (dst / "was_dir.txt").mkdir()

    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/srv/site"
    )

    assert (dst / "was_file").is_dir()
    assert (dst / "was_dir.txt").is_file()
    assert (dst / "was_dir.txt").read_text() == "now a file"
    assert stats.files_transferred == 1
    assert stats.deleted_entries == 2


def test_sync_upload_updates_changed_symlink_target(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    (src / "a.txt").write_text("a")
    _set_mtime(src / "a.txt")
    os.symlink("a.txt", src / "link")

    dst = remote / "srv" / "site"
    dst.mkdir(parents=True)
    os.symlink("/etc/hostname", dst / "link")

    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/srv/site"
    )

    assert os.readlink(dst / "link") == "a.txt"
    assert stats.symlinks_created == 1


def test_sync_upload_rejects_unsafe_inputs(roots) -> None:
    local, _remote = roots
    with pytest.raises(SshError, match="Local source directory not found"):
        ssh.sync_to_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            local / "missing", "/srv/site",
        )
    src = local / "site"
    src.mkdir()
    with pytest.raises(SshError, match="Refusing to sync to remote path"):
        ssh.sync_to_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/",
        )


def test_sync_upload_accepts_relative_remote_path(roots) -> None:
    """相对远端路径相对 SFTP 默认目录（家目录）解析，缺失时逐级创建。"""
    local, remote = roots
    src = local / "site"
    src.mkdir()
    _build_source(src)

    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "kube/site"
    )

    dst = remote / "kube" / "site"
    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "sub" / "b.txt").read_text() == "bb"
    link = dst / "link"
    assert link.is_symlink() and os.readlink(link) == "a.txt"
    assert stats.dirs_created >= 2  # kube/ 与 kube/site/ 逐级创建

    # 尾斜杠等价；再同步一次为 noop（相对路径下 size+mtime 判重同样生效）
    stats = ssh.sync_to_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "kube/site/"
    )
    assert stats.files_transferred == 0
    assert stats.dirs_created == 0
    assert stats.deleted_entries == 0


def test_sync_upload_fails_when_target_is_remote_file(roots) -> None:
    local, remote = roots
    src = local / "site"
    src.mkdir()
    (src / "a.txt").write_text("a")
    (remote / "srv").mkdir()
    (remote / "srv" / "site").write_text("i am a file")
    with pytest.raises(SshError, match="not a directory"):
        ssh.sync_to_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"), src, "/srv/site"
        )


# ---- 远端 -> 本地 ----


def test_sync_download_creates_full_mirror_then_noop(roots) -> None:
    local, remote = roots
    src = remote / "site"
    src.mkdir()
    _build_source(src)

    dst = local / "site"
    kwargs = {
        "instance": "vm1", "host": "10.0.0.5", "user": "cliff",
        "key_path": Path("/k/vm1"), "remote_path": "/site", "local_dir": dst,
    }
    stats = ssh.sync_from_remote(**kwargs)

    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "sub" / "b.txt").read_text() == "bb"
    link = dst / "link"
    assert link.is_symlink() and os.readlink(link) == "a.txt"
    assert stats.files_transferred == 2
    assert stats.deleted_entries == 0
    assert int((dst / "a.txt").stat().st_mtime) == FIXED_MTIME

    stats = ssh.sync_from_remote(**kwargs)
    assert stats.files_transferred == 0
    assert stats.deleted_entries == 0
    assert stats.symlinks_created == 0


def test_sync_download_deletes_extra_local_entries(roots) -> None:
    local, remote = roots
    src = remote / "site"
    src.mkdir()
    (src / "keep.txt").write_text("k")
    _set_mtime(src / "keep.txt")

    dst = local / "site"
    dst.mkdir()
    (dst / "extra").mkdir()
    (dst / "extra" / "deep.txt").write_text("d")
    (dst / "stale.txt").write_text("s")

    stats = ssh.sync_from_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
        "/site", dst,
    )

    assert not (dst / "extra").exists()
    assert not (dst / "stale.txt").exists()
    assert (dst / "keep.txt").read_text() == "k"
    assert stats.deleted_entries == 3  # deep.txt + extra/ + stale.txt


def test_sync_download_replaces_type_mismatches(roots) -> None:
    local, remote = roots
    src = remote / "site"
    src.mkdir()
    (src / "was_file").mkdir()
    (src / "was_dir").write_text("x")
    _set_mtime(src / "was_dir")

    dst = local / "site"
    dst.mkdir()
    (dst / "was_file").write_text("file before")
    (dst / "was_dir").mkdir()

    ssh.sync_from_remote(
        "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
        "/site", dst,
    )

    assert (dst / "was_file").is_dir()
    assert (dst / "was_dir").is_file()


def test_sync_download_rejects_root_target(roots) -> None:
    _local, _remote = roots
    with pytest.raises(SshError, match="Refusing to sync to local path"):
        ssh.sync_from_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/site", Path("/"),
        )


def test_sync_download_rejects_root_source(roots) -> None:
    _local, _remote = roots
    with pytest.raises(SshError, match="Refusing to sync to remote path"):
        ssh.sync_from_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/", Path("/tmp/out"),
        )


def test_sync_download_missing_remote_source_raises(roots) -> None:
    local, _remote = roots
    with pytest.raises(SshError, match="Remote path not found"):
        ssh.sync_from_remote(
            "vm1", "10.0.0.5", "cliff", Path("/k/vm1"),
            "/nope", local / "out",
        )


def test_sync_download_accepts_relative_remote_path(roots) -> None:
    """下载方向同样接受相对远端路径（相对 SFTP 默认目录/家目录）。"""
    local, remote = roots
    src = remote / "kube"
    src.mkdir()
    _build_source(src)

    dst = local / "mirror"
    kwargs = {
        "instance": "vm1", "host": "10.0.0.5", "user": "cliff",
        "key_path": Path("/k/vm1"), "remote_path": "kube/", "local_dir": dst,
    }
    stats = ssh.sync_from_remote(**kwargs)

    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "sub" / "b.txt").read_text() == "bb"
    link = dst / "link"
    assert link.is_symlink() and os.readlink(link) == "a.txt"
    assert stats.files_transferred == 2

    # 再同步一次为 noop
    stats = ssh.sync_from_remote(**kwargs)
    assert stats.files_transferred == 0
    assert stats.deleted_entries == 0


# ---- 编排层：方向解析与分发 ----


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(syncmod.domains, "lookup_domain", lambda *_: object())
    monkeypatch.setattr(syncmod.access, "_ensure_running", lambda *a: None)
    monkeypatch.setattr(
        syncmod.access,
        "wait_ssh_target",
        lambda *_a, **_k: ("10.0.0.5", "cliff", Path("/k/vm1")),
    )
    return monkeypatch


def test_sync_paths_rejects_two_remote_sides() -> None:
    with pytest.raises(ProvisionError, match="between two instances"):
        syncmod.sync_paths(object(), object(), "a:/x", "b:/y")


def test_sync_paths_rejects_two_local_sides() -> None:
    with pytest.raises(ProvisionError, match="instance path"):
        syncmod.sync_paths(object(), object(), "./a", "./b")


def test_sync_paths_upload_dispatch(ready, tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "site"
    src.mkdir()
    calls = []
    monkeypatch.setattr(
        syncmod.ssh,
        "sync_to_remote",
        lambda *a, **k: calls.append(a) or ssh.SyncStats(files_transferred=1),
    )

    assert (
        syncmod.sync_paths(object(), object(), str(src), "vm1:/srv/site") == 0
    )
    assert calls[0][0] == "vm1"
    assert calls[0][1] == "10.0.0.5"
    assert calls[0][4] == src
    assert calls[0][5] == "/srv/site"


def test_sync_paths_upload_requires_local_directory(
    ready, tmp_path: Path
) -> None:
    with pytest.raises(ProvisionError, match="Local source directory not found"):
        syncmod.sync_paths(
            object(), object(), str(tmp_path / "missing"), "vm1:/srv/site"
        )


def test_sync_paths_download_dispatch(ready, tmp_path: Path, monkeypatch) -> None:
    dst = tmp_path / "site"
    calls = []
    monkeypatch.setattr(
        syncmod.ssh,
        "sync_from_remote",
        lambda *a, **k: calls.append(a) or ssh.SyncStats(deleted_entries=2),
    )

    assert (
        syncmod.sync_paths(object(), object(), "vm1:/srv/site", str(dst)) == 0
    )
    assert calls[0][0] == "vm1"
    assert calls[0][4] == "/srv/site"
    assert calls[0][5] == dst


def test_sync_paths_unknown_instance_raises(monkeypatch, tmp_path: Path) -> None:
    src = tmp_path / "site"
    src.mkdir()
    monkeypatch.setattr(syncmod.domains, "lookup_domain", lambda *_: None)
    with pytest.raises(ProvisionError, match="does not exist"):
        syncmod.sync_paths(object(), object(), str(src), "ghost:/srv/site")
