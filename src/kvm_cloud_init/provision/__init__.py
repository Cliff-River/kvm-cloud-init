"""创建 / 生命周期 / 查询 / cloud-init 的编排层。

按职责拆分为多个子模块，公共 API 由本包统一导出：

- ``guard``：删除保护级别把关
- ``create``：幂等创建与镜像/种子上传
- ``lifecycle``：start / stop / reboot / status / destroy
- ``access``：SSH 目标解析与 shell / exec
- ``cloudinit_wait``：等待 cloud-init、状态查询与日志跟随
- ``listing``：list / templates
- ``images``：镜像目录 init / refresh / list / check / pull 与自动下载
"""

from __future__ import annotations

from .access import exec_instance, shell_instance, ssh_target
from .cloudinit_wait import cloud_init_log, cloud_init_status, wait_cloud_init
from .create import create_instance
from .guard import ensure_destroy_allowed, prompt_yes
from .images import (
    check_images,
    ensure_image,
    init_catalog,
    list_images,
    pull_images,
    refresh_catalog,
)
from .lifecycle import (
    destroy_instance,
    reboot_instance,
    start_instance,
    status_instance,
    stop_instance,
)
from .listing import list_instances, list_templates

__all__ = [
    "check_images",
    "cloud_init_log",
    "cloud_init_status",
    "create_instance",
    "destroy_instance",
    "ensure_destroy_allowed",
    "ensure_image",
    "exec_instance",
    "init_catalog",
    "list_images",
    "list_instances",
    "list_templates",
    "prompt_yes",
    "pull_images",
    "reboot_instance",
    "refresh_catalog",
    "shell_instance",
    "ssh_target",
    "start_instance",
    "status_instance",
    "stop_instance",
    "wait_cloud_init",
]
