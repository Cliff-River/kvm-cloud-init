"""provision.images 编排测试：目录生成、探测、拉取与 create 的镜像保障。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kvm_cloud_init import catalog, imagefetch
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ConfigError, ImageFetchError, ImageNotFound
from kvm_cloud_init.provision import images


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

    def fake_resolve(source, *, opener=None, sleep=None):
        calls["resolve"] += 1
        return target or _target(source.file)

    def fake_checksum(remote, *, opener=None, sleep=None):
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
    def raise_unreachable(source, *, opener=None, sleep=None):
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
    assert not (store.image_dir / ".base.qcow2.part").exists()


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
