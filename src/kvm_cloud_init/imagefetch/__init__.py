"""镜像获取能力包：HTTP、目录页解析、校验和、下载、解压与暂存。

原 ``imagefetch.py`` 的门面；``provision.images`` 与其他调用方继续通过
``imagefetch.<name>`` 访问公共 API。包内子模块之间用模块对象调用
（``from . import x`` + ``x.func``），便于测试替换底层实现。
"""

from __future__ import annotations

from .archive import ARCHIVE_SUFFIXES, extract_archive, is_archive
from .download import CHUNK_SIZE, download
from .http import (
    BACKOFF,
    RETRIES,
    TIMEOUT,
    USER_AGENT,
    fetch_text,
    new_client,
)
from .index import (
    basename,
    extract_hrefs,
    mtime_of,
    parse_index,
    pick_latest,
    version_of,
)
from .models import (
    Client,
    DownloadResult,
    Progress,
    RemoteImage,
    RemoteTarget,
)
from .resolve import fetch_checksum, parse_checksum, resolve_target
from .staging import (
    STAGING_DIRNAME,
    STAGING_LOCK,
    STAGING_MAX_AGE,
    staging_area,
    sweep_staging,
)
from .state import STATE_FILENAME, load_state, save_state

__all__ = [
    "ARCHIVE_SUFFIXES",
    "BACKOFF",
    "CHUNK_SIZE",
    "RETRIES",
    "STAGING_DIRNAME",
    "STAGING_LOCK",
    "STAGING_MAX_AGE",
    "STATE_FILENAME",
    "TIMEOUT",
    "USER_AGENT",
    "Client",
    "DownloadResult",
    "Progress",
    "RemoteImage",
    "RemoteTarget",
    "basename",
    "download",
    "extract_archive",
    "extract_hrefs",
    "fetch_checksum",
    "fetch_text",
    "is_archive",
    "load_state",
    "mtime_of",
    "new_client",
    "parse_checksum",
    "parse_index",
    "pick_latest",
    "resolve_target",
    "save_state",
    "staging_area",
    "sweep_staging",
    "version_of",
]
