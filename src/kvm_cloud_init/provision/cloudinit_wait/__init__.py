"""等待 cloud-init、查询状态与跟随日志。

原 ``cloudinit_wait.py`` 的门面：只导出公共函数与常量，
实现分布在 ``wait`` / ``query`` / ``render`` / ``log`` 子模块。
"""

from __future__ import annotations

from .constants import (
    LOG_FILE,
    OUTPUT_LOG,
    POLL_INTERVAL,
    PROGRESS_INTERVAL,
    ROWS,
    SPINNER_FRAMES,
    SPINNER_INTERVAL,
    STATUS_JSON_CMD,
    STATUS_LONG_CMD,
    VERBOSE_DEBUG_LINES,
    VERBOSE_INTERVAL,
    VERBOSE_LINES,
    VERBOSE_OUTPUT_LINES,
)
from .log import cloud_init_log, cloud_init_status
from .wait import wait_cloud_init

__all__ = [
    "LOG_FILE",
    "OUTPUT_LOG",
    "POLL_INTERVAL",
    "PROGRESS_INTERVAL",
    "ROWS",
    "SPINNER_FRAMES",
    "SPINNER_INTERVAL",
    "STATUS_JSON_CMD",
    "STATUS_LONG_CMD",
    "VERBOSE_DEBUG_LINES",
    "VERBOSE_INTERVAL",
    "VERBOSE_LINES",
    "VERBOSE_OUTPUT_LINES",
    "cloud_init_log",
    "cloud_init_status",
    "wait_cloud_init",
]
