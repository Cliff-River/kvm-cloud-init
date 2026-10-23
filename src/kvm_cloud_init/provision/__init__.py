"""创建 / 生命周期 / 查询 / cloud-init 的编排层。

按职责拆分为多个子模块，公共 API 由本包统一导出：

- ``guard``：删除保护级别把关
- ``create``：幂等创建与镜像/种子上传
- ``lifecycle``：start / stop / reboot / status / destroy
- ``access``：SSH 目标解析与 shell / exec
- ``cloudinit_wait``：等待 cloud-init、状态查询与日志跟随
- ``listing``：list / templates
"""

from __future__ import annotations

from .access import exec_instance, shell_instance, ssh_target
from .cloudinit_wait import cloud_init_log, cloud_init_status, wait_cloud_init
from .create import create_instance
from .guard import ensure_destroy_allowed, prompt_yes
from .lifecycle import (
    destroy_instance,
    reboot_instance,
    start_instance,
    status_instance,
    stop_instance,
)
from .listing import list_instances, list_templates

__all__ = [
    "create_instance",
    "destroy_instance",
    "exec_instance",
    "shell_instance",
    "ssh_target",
    "start_instance",
    "stop_instance",
    "reboot_instance",
    "status_instance",
    "wait_cloud_init",
    "cloud_init_status",
    "cloud_init_log",
    "list_instances",
    "list_templates",
    "ensure_destroy_allowed",
    "prompt_yes",
]
