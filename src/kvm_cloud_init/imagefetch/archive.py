"""压缩包解压：仅支持单文件发布物，全零块保持稀疏。"""

from __future__ import annotations

import contextlib
import os
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import ImageFetchError
from .download import CHUNK_SIZE

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from typing import BinaryIO

#: 支持解压的发布物后缀（Kali 的云镜像就是 .tar.xz 包）
ARCHIVE_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tgz", ".tar", ".zip")
_TAR_MODES = {
    ".tar.xz": "r:xz",
    ".tar.gz": "r:gz",
    ".tgz": "r:gz",
    ".tar.bz2": "r:bz2",
    ".tar": "r:",
}


def is_archive(filename: str) -> bool:
    """判断发布物是否为受支持的压缩包。"""
    lowered = filename.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def extract_archive(archive: Path, dest: Path, *, filename: str | None = None) -> str:
    """把压缩包内唯一的普通文件解到 ``dest``，返回包内文件名。

    只支持单文件包（Kali 的 ``.tar.xz`` 里就是 ``disk.raw``）：多文件说明
    发布物结构超出预期，直接失败以免解出错误的镜像。解压时全零块改用
    ``seek`` 前进，裸盘的稀疏区域不会真的占用磁盘。

    ``filename`` 是发布物原始文件名：暂存文件名为 ``source``（无后缀），
    无法据此判断压缩格式，故由调用方传入上游名字。
    """
    name = filename or archive.name
    lowered = name.lower()
    with contextlib.ExitStack() as stack:
        if lowered.endswith(".zip"):
            zf = stack.enter_context(zipfile.ZipFile(archive))
            names = [info.filename for info in zf.infolist() if not info.is_dir()]
            member = _only_member(names, name)
            stream: BinaryIO = stack.enter_context(zf.open(member))
        else:
            mode = next(
                (value for suffix, value in _TAR_MODES.items()
                 if lowered.endswith(suffix)),
                None,
            )
            if mode is None:
                raise ImageFetchError(
                    f"Unsupported archive format: {name}"
                )
            tf = stack.enter_context(tarfile.open(archive, mode))
            member = _only_member(
                [item.name for item in tf.getmembers() if item.isreg()], name
            )
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ImageFetchError(
                    f"Cannot read {member} from archive {name}"
                )
            stream = stack.enter_context(extracted)
        _write_sparse(stream, dest)
    return member


def _only_member(names: list[str], archive_name: str) -> str:
    if len(names) != 1:
        raise ImageFetchError(
            f"Archive {archive_name} must contain exactly one file, found "
            f"{len(names)}: {', '.join(names[:5]) or 'none'}"
        )
    return names[0]


def _write_sparse(stream: BinaryIO, dest: Path) -> None:
    """边读边写；整块全零时只前进写入位置，保持文件稀疏。"""
    with dest.open("wb") as out:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            if chunk.strip(b"\x00"):
                out.write(chunk)
            else:
                out.seek(len(chunk), os.SEEK_CUR)
        out.truncate()
