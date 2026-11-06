"""下载暂存目录：与最终镜像同文件系统，并用 ``flock`` 探活。"""

from __future__ import annotations

import contextlib
import fcntl
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

STAGING_DIRNAME = ".staging"
STAGING_LOCK = ".lock"
#: 暂存目录超过该秒数且无人持锁时视为残留（覆盖建目录到加锁的瞬时窗口）
STAGING_MAX_AGE = 3600.0


@contextlib.contextmanager
def staging_area(image_dir: Path) -> Iterator[Path]:
    """在 ``image_dir/.staging`` 下创建临时目录，并持有排他 ``flock``。

    暂存目录与最终镜像位于同一文件系统，``os.replace`` 是一次真正的 rename。
    退出上下文时（含异常与 Ctrl+C）整目录删除；进程被强杀时由
    :func:`sweep_staging` 在后续运行中回收。
    """
    root = image_dir / STAGING_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=root, ignore_cleanup_errors=True
    ) as temporary:
        path = Path(temporary)
        lock = (path / STAGING_LOCK).open("wb")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield path
        finally:
            lock.close()


def sweep_staging(
    image_dir: Path,
    *,
    max_age: float = STAGING_MAX_AGE,
    now: float | None = None,
) -> int:
    """回收无人使用且超过 ``max_age`` 的暂存目录，返回删除数量。

    活跃下载持有 ``.lock`` 的排他锁：清扫时尝试非阻塞加锁，拿不到锁就跳过。
    年龄阈值用于覆盖"目录已创建但尚未加锁"的瞬时窗口，也避免刚启动的下载
    被误删。清理是 best-effort，失败只跳过，不抛出。
    """
    root = image_dir / STAGING_DIRNAME
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0

    current = time.time() if now is None else now
    removed = 0
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if current - entry.stat().st_mtime < max_age:
                continue
            with (entry / STAGING_LOCK).open("a+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed += 1
        except OSError:
            continue
    return removed
