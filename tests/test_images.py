"""provision.images 编排测试：目录生成、探测、拉取与 create 的镜像保障。"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from rich.console import Console

from kvm_cloud_init import catalog, imagefetch, imagefmt, qcow2
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ConfigError, ImageFetchError, ImageNotFound
from kvm_cloud_init.provision import images
from kvm_cloud_init.provision.progress import DownloadProgress, _ResizeAwareLive


def _target(name: str = "base.qcow2") -> imagefetch.RemoteTarget:
    return imagefetch.RemoteTarget(
        filename=name,
        urls=(f"https://example.invalid/{name}",),
        checksum_url=f"https://example.invalid/{name}.sha256",
        version="1.0",
    )


def _patch_remote(
    monkeypatch,
    *,
    checksum: str = "a" * 64,
    target: imagefetch.RemoteTarget | None = None,
) -> dict[str, int]:
    """替换网络解析与校验和获取，返回调用计数。"""
    calls = {"resolve": 0, "checksum": 0}

    def fake_resolve(source, *, client=None, sleep=None):
        calls["resolve"] += 1
        return target or _target(source.file)

    def fake_checksum(remote, *, client=None, sleep=None):
        calls["checksum"] += 1
        return checksum

    monkeypatch.setattr(images.imagefetch, "resolve_target", fake_resolve)
    monkeypatch.setattr(images.imagefetch, "fetch_checksum", fake_checksum)
    return calls


def _patch_download(monkeypatch, body: bytes = b"image") -> list:
    downloaded: list = []

    def fake_download(target, part_path, *, algo="sha256", expected=None, **kwargs):
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(body)
        downloaded.append((target, expected))
        return imagefetch.DownloadResult(
            checksum=hashlib.sha256(body).hexdigest(), size=len(body)
        )

    monkeypatch.setattr(images.imagefetch, "download", fake_download)
    return downloaded


# ---- init / refresh ----


def test_init_refuses_existing_and_force_rebuilds(
    project_dir: Path, capsys
) -> None:
    user = project_dir / catalog.USER_CATALOG_FILENAME
    user.write_text("# user file\n")
    before = (project_dir / catalog.CATALOG_FILENAME).read_text()

    assert images.init_catalog(project_dir) == 1
    assert "already exists" in capsys.readouterr().err
    assert (project_dir / catalog.CATALOG_FILENAME).read_text() == before

    (project_dir / catalog.CATALOG_FILENAME).write_text("stale: {}\n")
    assert images.init_catalog(project_dir, force=True) == 0
    text = (project_dir / catalog.CATALOG_FILENAME).read_text()
    assert "almalinux-10:" in text
    assert user.read_text() == "# user file\n"


def test_init_generates_missing_catalog(tmp_path: Path, capsys) -> None:
    assert images.init_catalog(tmp_path) == 0
    assert (tmp_path / catalog.CATALOG_FILENAME).exists()
    assert "Generated image catalog" in capsys.readouterr().out


def test_refresh_writes_upstream_metadata_and_skips_archived(
    tmp_path: Path, monkeypatch
) -> None:
    calls = _patch_remote(monkeypatch)
    assert images.refresh_catalog(tmp_path) == 0
    text = (tmp_path / catalog.CATALOG_FILENAME).read_text()
    assert "upstream_file: AlmaLinux-8-GenericCloud.x86_64.qcow2" in text
    assert "upstream_status: ok" in text
    # 非 archived 条目数（预设中 archived 的条目被跳过）
    assert calls["resolve"] == sum(1 for entry in catalog.PRESET if not entry.archived)


def test_refresh_records_unreachable_and_continues(tmp_path: Path, monkeypatch) -> None:
    def raise_unreachable(source, *, client=None, sleep=None):
        raise ImageFetchError("down")

    monkeypatch.setattr(images.imagefetch, "resolve_target", raise_unreachable)
    assert images.refresh_catalog(tmp_path) == 1
    text = (tmp_path / catalog.CATALOG_FILENAME).read_text()
    assert "upstream_status: unreachable" in text


def test_refresh_include_archived(tmp_path: Path, monkeypatch) -> None:
    calls = _patch_remote(monkeypatch)
    assert images.refresh_catalog(tmp_path, include_archived=True) == 0
    assert calls["resolve"] == len(catalog.PRESET)


# ---- list / check / pull 选择 ----


def test_list_images_reads_catalog_offline(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        images.imagefetch,
        "resolve_target",
        lambda *a, **k: pytest.fail("list must not touch the network"),
    )
    rows = images.list_images(store)
    assert ("base", "system", "missing", "url", "-", "-") in rows


def test_check_defaults_to_template_images(store: ConfigStore, monkeypatch) -> None:
    _patch_remote(monkeypatch, checksum="a" * 64)
    (store.image_dir / "base.qcow2").write_bytes(b"image")
    expected = hashlib.sha256(b"image").hexdigest()
    monkeypatch.setattr(images, "_expected_checksum", lambda *a, **k: expected)
    assert images.check_images(store, []) == 1  # ubuntu/misc 缺失
    assert images.check_images(store, ["base"]) == 0


def test_check_reports_update_available(store: ConfigStore, monkeypatch, capsys) -> None:
    _patch_remote(monkeypatch)
    (store.image_dir / "base.qcow2").write_bytes(b"old")
    monkeypatch.setattr(images, "_expected_checksum", lambda *a, **k: "b" * 64)
    assert images.check_images(store, ["base"]) == 1
    assert "update available" in capsys.readouterr().out


def test_check_all_skips_archived(store: ConfigStore, monkeypatch, capsys) -> None:
    store.images["old"] = type(store.images["base"])(
        id="old", file="old.qcow2", kind="url", urls=("https://x/old.qcow2",),
        checksum_url="https://x/old.sha256", archived=True,
    )
    checked: list[str] = []
    monkeypatch.setattr(images.imagefetch, "resolve_target", lambda s, **k: checked.append(s.id) or _target(s.file))
    monkeypatch.setattr(images.imagefetch, "fetch_checksum", lambda *a, **k: "a" * 64)
    images.check_images(store, [], all_entries=True)
    assert "old" not in checked


def test_pull_requires_ids(store: ConfigStore) -> None:
    with pytest.raises(ConfigError, match="Unknown image or template"):
        images._select_ids(store, ["ghost"])


def test_pull_all_uses_template_ids_exactly(store: ConfigStore, monkeypatch) -> None:
    pulled: list[str] = []
    monkeypatch.setattr(images, "_pull_one", lambda store, image_id, **k: pulled.append(image_id))
    assert images.pull_images(store, [], all_templates=True) == 0
    assert pulled == ["base", "ubuntu", "misc"]


def test_pull_accepts_template_names(store: ConfigStore, monkeypatch) -> None:
    pulled: list[str] = []
    monkeypatch.setattr(images, "_pull_one", lambda store, image_id, **k: pulled.append(image_id))
    assert images.pull_images(store, ["t1", "t2"]) == 0
    assert pulled == ["base", "ubuntu"]


def test_pull_continues_after_failure(store: ConfigStore, monkeypatch, capsys) -> None:
    pulled: list[str] = []

    def fake_pull(store, image_id, **kwargs):
        pulled.append(image_id)
        if image_id == "ubuntu":
            raise ImageFetchError("boom")

    monkeypatch.setattr(images, "_pull_one", fake_pull)
    assert images.pull_images(store, [], all_templates=True) == 1
    assert pulled == ["base", "ubuntu", "misc"]
    assert "Images processed: 2 ok, 1 failed." in capsys.readouterr().out


def test_pull_skips_up_to_date_and_force_redownloads(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    body = b"same"
    disk = store.image_dir / "base.qcow2"
    disk.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    _patch_remote(monkeypatch, checksum=expected)
    downloaded = _patch_download(monkeypatch)
    progress = []

    assert images.pull_images(store, ["base"], progress_factory=lambda name: progress.append(name) and None) == 0
    assert downloaded == []
    assert "up to date" in capsys.readouterr().out

    assert images.pull_images(store, ["base"], force=True, progress_factory=lambda name: None) == 0
    assert len(downloaded) == 1
    assert disk.read_bytes() == b"image"


# ---- 压缩包解压 + 格式转换 ----


def _register_kali(store: ConfigStore) -> None:
    store.images["kali"] = type(store.images["base"])(
        id="kali",
        file="kali.qcow2",
        kind="url",
        urls=("https://example.invalid/kali.tar.xz",),
        checksum_url="https://example.invalid/SHA256SUMS",
        format="qcow2",
    )


def _kali_target() -> imagefetch.RemoteTarget:
    return imagefetch.RemoteTarget(
        filename="kali-linux-2026.3-cloud-genericcloud-amd64.tar.xz",
        urls=("https://example.invalid/kali.tar.xz",),
        checksum_url="https://example.invalid/SHA256SUMS",
        version="2026.3",
    )


def _patch_archive_download(monkeypatch, payload: bytes) -> None:
    def fake_download(target, part_path, *, algo="sha256", expected=None, **kwargs):
        part_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(part_path, "w:xz") as tf:
            info = tarfile.TarInfo("disk.raw")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return imagefetch.DownloadResult(checksum="b" * 64, size=len(payload))

    monkeypatch.setattr(images.imagefetch, "download", fake_download)


def test_pull_extracts_archive_and_converts_to_declared_format(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    _register_kali(store)
    _patch_remote(monkeypatch, target=_kali_target(), checksum="b" * 64)
    raw = b"A" * imagefmt.CLUSTER_SIZE + b"\x00" * imagefmt.CLUSTER_SIZE
    _patch_archive_download(monkeypatch, raw)

    assert images.pull_images(store, ["kali"]) == 0

    disk = store.image_path("kali")
    assert imagefmt.detect(disk) == "qcow2"
    assert qcow2.read_info(str(disk)).virtual_size == len(raw)
    assert not list(store.image_dir.glob(".kali*")), "临时文件必须清理"
    staging = store.image_dir / imagefetch.STAGING_DIRNAME
    assert not list(staging.iterdir()), "暂存目录必须清理"
    state = imagefetch.load_state(store.image_dir)
    assert state["kali"]["format"] == "qcow2"
    assert state["kali"]["source_format"] == "raw"
    assert state["kali"]["transformed"] == "true"
    assert "Extracting" in capsys.readouterr().out


def test_pull_skips_up_to_date_transformed_image(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    _register_kali(store)
    disk = store.image_path("kali")
    disk.write_bytes(b"converted")
    imagefetch.save_state(
        store.image_dir,
        {"kali": {"upstream_file": _kali_target().filename, "transformed": "true"}},
    )
    _patch_remote(monkeypatch, target=_kali_target(), checksum="b" * 64)
    downloaded = _patch_download(monkeypatch)

    assert images.pull_images(store, ["kali"]) == 0
    assert downloaded == []
    assert "up to date" in capsys.readouterr().out


def test_check_compares_upstream_name_for_transformed_image(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    _register_kali(store)
    store.image_path("kali").write_bytes(b"converted")
    _patch_remote(monkeypatch, target=_kali_target(), checksum="b" * 64)

    imagefetch.save_state(
        store.image_dir,
        {"kali": {"upstream_file": _kali_target().filename, "transformed": "true"}},
    )
    assert images.check_images(store, ["kali"]) == 0
    assert "up to date" in capsys.readouterr().out

    imagefetch.save_state(
        store.image_dir,
        {"kali": {"upstream_file": "kali-linux-2025.4-cloud-genericcloud-amd64.tar.xz"}},
    )
    assert images.check_images(store, ["kali"]) == 1
    assert "update available" in capsys.readouterr().out


# ---- ensure_image 决策表 ----


def test_ensure_image_existing_never_touches_network(
    store: ConfigStore, monkeypatch
) -> None:
    disk = store.image_dir / "base.qcow2"
    disk.write_bytes(b"local")
    monkeypatch.setattr(
        images.imagefetch,
        "resolve_target",
        lambda *a, **k: pytest.fail("existing image must not hit the network"),
    )
    assert images.ensure_image(store, "base") == disk
    assert images.ensure_image(store, "base", policy="local") == disk


def test_ensure_image_local_policy_missing_raises(store: ConfigStore, monkeypatch) -> None:
    monkeypatch.setattr(
        images.imagefetch,
        "resolve_target",
        lambda *a, **k: pytest.fail("local policy must not hit the network"),
    )
    with pytest.raises(ImageNotFound, match="image base"):
        images.ensure_image(store, "base", policy="local")


def test_ensure_image_local_policy_from_defaults(project_dir: Path) -> None:
    (project_dir / "default.conf").write_text(
        (project_dir / "default.conf").read_text() + "image_policy: local\n"
    )
    store = ConfigStore.load(project_dir)
    with pytest.raises(ImageNotFound):
        images.ensure_image(store, "base")


def test_ensure_image_auto_downloads_and_records_state(
    store: ConfigStore, monkeypatch
) -> None:
    _patch_remote(monkeypatch, checksum="a" * 64)
    downloaded = _patch_download(monkeypatch)
    path = images.ensure_image(store, "base")
    assert path == store.image_dir / "base.qcow2"
    assert path.read_bytes() == b"image"
    assert len(downloaded) == 1
    state = imagefetch.load_state(store.image_dir)
    assert state["base"]["file"] == "base.qcow2"
    assert state["base"]["checksum"] == hashlib.sha256(b"image").hexdigest()
    staging = store.image_dir / imagefetch.STAGING_DIRNAME
    assert not list(staging.iterdir()), "暂存目录必须清理"


def test_ensure_image_insecure_without_checksum_warns(
    store: ConfigStore, monkeypatch, capsys
) -> None:
    source = store.images["base"]
    store.images["base"] = type(source)(
        id=source.id, file=source.file, kind=source.kind, urls=source.urls,
        checksum_url=None, insecure=True,
    )
    monkeypatch.setattr(images.imagefetch, "resolve_target", lambda s, **k: _target(s.file))
    monkeypatch.setattr(images.imagefetch, "fetch_checksum", lambda *a, **k: None)
    _patch_download(monkeypatch)
    images.ensure_image(store, "base")
    assert "Warning" in capsys.readouterr().err


def test_ensure_image_requires_checksum_when_secure(
    store: ConfigStore, monkeypatch
) -> None:
    source = store.images["base"]
    store.images["base"] = type(source)(
        id=source.id, file=source.file, kind=source.kind, urls=source.urls,
        checksum_url=None, insecure=False,
    )
    monkeypatch.setattr(images.imagefetch, "resolve_target", lambda s, **k: _target(s.file))
    monkeypatch.setattr(images.imagefetch, "fetch_checksum", lambda *a, **k: None)
    with pytest.raises(ImageFetchError, match="no checksum source"):
        images.ensure_image(store, "base")


# ---- 下载进度 ----


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_download_progress_plain_shows_current_and_total() -> None:
    stream = io.StringIO()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, None)
    progress.update(0, 4 * 1024 * 1024)
    progress.update(2 * 1024 * 1024, 4 * 1024 * 1024)
    progress.update(4 * 1024 * 1024, 4 * 1024 * 1024)
    progress.close()
    text = stream.getvalue()
    assert "0.0 MiB / 4.0 MiB (0%)" in text
    assert "4.0 MiB / 4.0 MiB (100%)" in text


def test_download_progress_tty_renders_rich_bar(monkeypatch) -> None:
    _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, 4 * 1024 * 1024)
    progress._progress.refresh()
    first = stream.getvalue()
    assert "Downloading base" in first
    assert "0.0/4.2 MB" in first
    # rich 的就地刷新控制序列
    assert "\x1b[2K" in first

    progress.update(4 * 1024 * 1024, 4 * 1024 * 1024)
    progress.close()
    assert "4.2/4.2 MB" in stream.getvalue()[len(first):]


def test_download_progress_unknown_total_avoids_question_mark(monkeypatch) -> None:
    _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, None)
    progress.update(1024 * 1024, None)
    progress._progress.refresh()
    progress.close()
    text = stream.getvalue()
    assert "? MB" not in text
    assert "1.0 MB" in text


def test_download_progress_close_keeps_current_progress(monkeypatch) -> None:
    _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, 4 * 1024 * 1024)
    progress.update(1024 * 1024, 4 * 1024 * 1024)
    progress.close()
    text = stream.getvalue()
    # 最终一帧停在 25%（1.0/4.2 MB），没有被补满
    assert "1.0/4.2 MB" in text
    assert "4.2/4.2 MB" not in text


def test_download_progress_resize_clears_reflowed_rows(monkeypatch) -> None:
    width = _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, 100 * 1024 * 1024)
    progress._progress.refresh()
    first = stream.getvalue()
    # 80 列时只有一行，rich 正常定位不需要上移
    assert "\x1b[1A" not in first

    width["value"] = 20
    progress.update(50 * 1024 * 1024, 100 * 1024 * 1024)
    progress._progress.refresh()
    after = stream.getvalue()[len(first):]
    # 缩窄后 reflow 成多行，自定义 Live 逐行清除旧快照
    assert "\x1b[1A" in after
    progress.close()


def test_resize_aware_live_erases_reflow_rows(monkeypatch) -> None:
    width = _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    console = Console(file=stream)
    live = _ResizeAwareLive(
        get_renderable=lambda: "x" * 70,
        console=console,
        auto_refresh=False,
    )
    live.start(refresh=True)
    assert "\x1b[1A" not in stream.getvalue()

    width["value"] = 20  # 70 列内容 reflow 成 4 个视觉行
    first = stream.getvalue()
    live.refresh()
    second = stream.getvalue()[len(first):]
    # 4 个视觉行：先清当前行再上移 3 次，不能有第 4 次上移抹到上方保留行
    assert second.count("\x1b[1A") == 3
    live.stop()


def test_resize_aware_live_widening_keeps_native_positioning(monkeypatch) -> None:
    width = _patch_console_width(monkeypatch, 20)
    stream = _TtyStream()
    console = Console(file=stream)
    live = _ResizeAwareLive(
        get_renderable=lambda: "x" * 70,
        console=console,
        auto_refresh=False,
    )
    live.start(refresh=True)  # 20 列时占 4 行
    first = stream.getvalue()

    width["value"] = 80  # 变宽：显式换行不会被合并，rich 原生定位即正确
    live.refresh()
    second = stream.getvalue()[len(first):]
    # 只应有原生的 height-1=3 次上移，不是 hook 的 4 次
    assert second.count("\x1b[1A") == 3
    live.stop()


def test_download_progress_close_is_idempotent(monkeypatch) -> None:
    _patch_console_width(monkeypatch, 80)
    stream = _TtyStream()
    progress = DownloadProgress("base", stream=stream)
    progress.update(0, 1024)
    progress.close()
    assert progress._progress is None
    progress.close()
    progress.update(1024, 1024)


def _patch_console_width(monkeypatch, columns: int) -> dict[str, int]:
    """让 rich Console.size 返回可变宽度，并模拟正常终端环境。"""
    from rich.console import Console as RichConsole
    from rich.console import ConsoleDimensions

    monkeypatch.setenv("TERM", "xterm-256color")
    width = {"value": columns}
    monkeypatch.setattr(
        RichConsole,
        "size",
        property(lambda self: ConsoleDimensions(width["value"], 24)),
    )
    return width
