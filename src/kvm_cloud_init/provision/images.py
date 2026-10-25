"""镜像目录编排：初始化、刷新、列出、探测与拉取，以及 create 的镜像保障。"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .. import catalog, imagefetch
from ..config import ConfigStore, ImageSource
from ..errors import ConfigError, ImageFetchError, ImageNotFound

Client = imagefetch.Client
Progress = imagefetch.Progress


# --------------------------------------------------------------------------- #
# 目录初始化 / 刷新
# --------------------------------------------------------------------------- #


def init_catalog(root: Path, *, force: bool = False) -> int:
    """从内置预设生成 images.conf；已存在时默认拒绝覆盖。"""
    path = root / catalog.CATALOG_FILENAME
    if path.exists() and not force:
        print(
            f"Error: {path} already exists; use 'kvmci images refresh' to rebuild it "
            "or add --force to overwrite.",
            file=sys.stderr,
        )
        return 1
    _write_catalog(path, catalog.render_catalog())
    print(f"Generated image catalog: {path}")
    return 0


def refresh_catalog(
    root: Path,
    *,
    include_archived: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """重建 images.conf，并把上游最新文件名/版本写回生成文件的元数据字段。"""
    probes: dict[str, dict[str, str]] = {}
    attempted = 0
    reachable = 0
    for entry in catalog.PRESET:
        if entry.archived and not include_archived:
            continue
        attempted += 1
        source = _preset_source(entry)
        try:
            target = imagefetch.resolve_target(source, client=client, sleep=sleep)
            checksum = imagefetch.fetch_checksum(target, client=client, sleep=sleep)
        except ImageFetchError as exc:
            probes[entry.id] = {
                "upstream_status": "unreachable",
                "checked_at": _timestamp(),
            }
            print(f"{entry.id}: unreachable ({exc})")
            continue
        reachable += 1
        probes[entry.id] = {
            "upstream_file": target.filename,
            "upstream_version": target.version,
            "upstream_status": "ok",
            "checked_at": _timestamp(),
        }
        suffix = f" {checksum[:12]}" if checksum else ""
        print(f"{entry.id}: {target.filename}{suffix}")

    path = root / catalog.CATALOG_FILENAME
    _write_catalog(path, catalog.render_catalog(probes))
    print(f"Refreshed image catalog: {path} ({reachable}/{attempted} sources reachable)")
    return 1 if attempted and reachable == 0 else 0


def _preset_source(entry: catalog.PresetEntry) -> ImageSource:
    return ImageSource(
        id=entry.id,
        file=entry.file,
        kind=entry.kind,
        urls=entry.urls,
        pages=entry.pages,
        match=entry.match or None,
        checksum_url=entry.checksum_url or None,
        checksum_file=entry.checksum_file or None,
        checksum_match=entry.checksum_match or None,
        checksum_algo=entry.checksum_algo,
        archived=entry.archived,
        arch=entry.arch,
    )


def _write_catalog(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# --------------------------------------------------------------------------- #
# list / check / pull
# --------------------------------------------------------------------------- #


def list_images(store: ConfigStore) -> list[tuple[str, str, str, str, str, str]]:
    """返回 (ID, Origin, Local, Kind, Catalog ver., Local ver.) 行，不触网。"""
    state = imagefetch.load_state(store.image_dir)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for image_id, source in store.images.items():
        disk = store.image_path(image_id)
        local_version = _state_value(state, image_id, "version")
        rows.append(
            (
                image_id,
                source.origin,
                "present" if disk.is_file() else "missing",
                source.kind,
                source.upstream_version or "-",
                local_version or "-",
            )
        )
    return rows


def check_images(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_entries: bool = False,
    include_archived: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """只探测上游，不下载；存在可更新或失败时返回 1。"""
    ids = _select_ids(
        store,
        tokens,
        all_entries=all_entries,
        all_templates=not tokens and not all_entries,
        include_archived=include_archived,
    )
    state = imagefetch.load_state(store.image_dir)
    failed = False
    for image_id in ids:
        source = store.images[image_id]
        disk = store.image_path(image_id)
        local_version = _state_value(state, image_id, "version") or "-"
        try:
            target = imagefetch.resolve_target(source, client=client, sleep=sleep)
            expected = _expected_checksum(source, target, client=client, sleep=sleep)
        except ImageFetchError as exc:
            print(f"{image_id}: unreachable ({exc})")
            failed = True
            continue
        upstream = source.upstream_version or target.version
        if not disk.is_file():
            print(f"{image_id}: missing (upstream {upstream})")
            failed = True
            continue
        if expected is None:
            print(f"{image_id}: unknown (no checksum; local {local_version})")
            continue
        if _hash_file(disk, source.checksum_algo) == expected:
            print(f"{image_id}: up to date ({local_version})")
        else:
            print(
                f"{image_id}: update available "
                f"(local {local_version}, upstream {upstream})"
            )
            failed = True
    return 1 if failed else 0


def pull_images(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_templates: bool = False,
    force: bool = False,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress_factory: Callable[[str], Progress | None] = lambda name: progress_printer(
        name
    ),
) -> int:
    """下载或更新指定镜像；单项失败不中断，全部处理完后返回退出码。"""
    ids = _select_ids(store, tokens, all_templates=all_templates)
    failures: list[str] = []
    for image_id in ids:
        try:
            _pull_one(
                store,
                image_id,
                force=force,
                client=client,
                sleep=sleep,
                progress=progress_factory(image_id),
            )
        except (ImageFetchError, ImageNotFound, OSError) as exc:
            failures.append(image_id)
            print(f"Error: failed to pull {image_id}: {exc}", file=sys.stderr)
    if len(ids) > 1:
        print(f"Images processed: {len(ids) - len(failures)} ok, {len(failures)} failed.")
    return 1 if failures else 0


def _pull_one(
    store: ConfigStore,
    image_id: str,
    *,
    force: bool,
    client: Client | None,
    sleep: Callable[[float], None],
    progress: Progress | None,
) -> None:
    source = store.images[image_id]
    disk = store.image_path(image_id)
    target = imagefetch.resolve_target(source, client=client, sleep=sleep)
    expected = _expected_checksum(source, target, client=client, sleep=sleep)

    if disk.is_file() and not force:
        if expected is not None and _hash_file(disk, source.checksum_algo) == expected:
            print(f"Image {image_id} is up to date ({disk.name}).")
            return
        if expected is None:
            state = imagefetch.load_state(store.image_dir)
            if _state_value(state, image_id, "upstream_file") == target.filename:
                print(f"Image {image_id} is up to date ({disk.name}).")
                return

    _download(store, source, target, expected, progress=progress, client=client)


def ensure_image(
    store: ConfigStore,
    image_id: str,
    *,
    policy: str | None = None,
    client: Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Progress | None = None,
) -> Path:
    """确保镜像在本地存在：缺失时按策略自动下载，或按 local 策略直接失败。"""
    disk = store.image_path(image_id)
    if disk.is_file():
        return disk

    effective = policy or store.defaults.image_policy
    if effective == "local":
        raise ImageNotFound(
            f"Source image does not exist: {disk} (image {image_id}); "
            f"run 'kvmci images pull {image_id}' or set image_policy: auto"
        )

    source = store.images[image_id]
    target = imagefetch.resolve_target(source, client=client, sleep=sleep)
    expected = _expected_checksum(source, target, client=client, sleep=sleep)
    print(f"Image {image_id} is missing locally; downloading {target.filename} ...")
    return _download(store, source, target, expected, progress=progress, client=client)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _select_ids(
    store: ConfigStore,
    tokens: list[str],
    *,
    all_entries: bool = False,
    all_templates: bool = False,
    include_archived: bool = False,
) -> list[str]:
    """把镜像 ID / 模板名解析为镜像 ID 列表（去重、保持顺序）。"""
    if all_entries:
        return [
            image_id
            for image_id, source in store.images.items()
            if include_archived or not source.archived
        ]
    if all_templates:
        return store.template_image_ids()
    chosen: list[str] = []
    for token in tokens:
        if token in store.images:
            image_id = token
        elif token in store.templates:
            image_id = store.templates[token].image
        else:
            raise ConfigError(
                f"Unknown image or template: {token} (see 'kvmci images list')"
            )
        if image_id not in chosen:
            chosen.append(image_id)
    return chosen


def _expected_checksum(
    source: ImageSource,
    target: imagefetch.RemoteTarget,
    *,
    client: Client | None,
    sleep: Callable[[float], None],
) -> str | None:
    try:
        checksum = imagefetch.fetch_checksum(target, client=client, sleep=sleep)
    except ImageFetchError:
        if source.insecure:
            print(
                f"Warning: no checksum available for {source.id}; continuing "
                "because insecure is enabled.",
                file=sys.stderr,
            )
            return None
        raise
    if checksum is None:
        if source.insecure:
            print(
                f"Warning: no checksum configured for {source.id}; continuing "
                "because insecure is enabled.",
                file=sys.stderr,
            )
            return None
        raise ImageFetchError(
            f"Image {source.id} has no checksum source; set insecure: true to "
            "download without verification"
        )
    return checksum


def _download(
    store: ConfigStore,
    source: ImageSource,
    target: imagefetch.RemoteTarget,
    expected: str | None,
    *,
    progress: Progress | None,
    client: Client | None,
) -> Path:
    disk = store.image_path(source.id)
    disk.parent.mkdir(parents=True, exist_ok=True)
    part = disk.with_name(f".{disk.name}.part")
    print(f"Downloading image {source.id} ({target.filename}) ...")
    result = imagefetch.download(
        target,
        part,
        algo=source.checksum_algo,
        expected=expected,
        client=client,
        progress=progress,
    )
    os.replace(part, disk)

    state = imagefetch.load_state(disk.parent)
    state[source.id] = {
        "file": disk.name,
        "url": target.urls[0],
        "version": target.version,
        "upstream_file": target.filename,
        "checksum_algo": source.checksum_algo,
        "checksum": result.checksum,
        "size": result.size,
        "fetched_at": _timestamp(),
    }
    imagefetch.save_state(disk.parent, state)
    print(f"Image {source.id} saved to {disk}.")
    return disk


def _hash_file(path: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(imagefetch.CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_value(
    state: dict[str, dict[str, object]], image_id: str, key: str
) -> str | None:
    entry = state.get(image_id)
    if not entry:
        return None
    value = entry.get(key)
    return str(value) if value is not None else None


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def progress_printer(label: str) -> Progress:
    """下载进度输出：TTY 单行原地刷新，非 TTY 每 15 秒追加一行。"""
    last_report = 0.0

    def report(done: int, total: int | None) -> None:
        nonlocal last_report
        now = time.monotonic()
        if sys.stdout.isatty():
            text = f"Downloading {label}: {_fmt_size(done)}"
            if total:
                text += f" / {_fmt_size(total)} ({done * 100 // total}%)"
            print("\r" + text + " " * 8, end="", flush=True)
            if total and done >= total:
                print()
            return
        if now - last_report < 15:
            return
        last_report = now
        suffix = f" / {_fmt_size(total)}" if total else ""
        print(f"Downloading {label}: {_fmt_size(done)}{suffix}", flush=True)

    return report


def _fmt_size(size: int) -> str:
    mib = size / (1024 * 1024)
    return f"{mib:.1f} MiB"
