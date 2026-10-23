"""list / templates 查询。"""

from __future__ import annotations

import libvirt

from ..config import ConfigStore
from ..hv.client import DOMAIN_STATE_NAMES


def list_instances(
    store: ConfigStore, conn: libvirt.virConnect
) -> list[tuple[str, str, str]]:
    """返回 (实例名, 模板名或 '-', 状态) 行，含已登记实例和未登记域。"""
    live_domains = {dom.name(): dom for dom in conn.listAllDomains(0)}
    rows: list[tuple[str, str, str]] = []

    for name, inst in store.instances.items():
        rows.append((name, inst.template, _domain_state(live_domains.get(name))))

    for name, dom in live_domains.items():
        if name not in store.instances:
            rows.append((name, "-", _domain_state(dom)))
    return rows


def list_templates(store: ConfigStore) -> list[tuple[str, str, str]]:
    """返回 (模板名, 镜像文件名, os_variant) 行。"""
    return [(name, tpl.image, tpl.os_variant) for name, tpl in store.templates.items()]


def _domain_state(dom: libvirt.virDomain | None) -> str:
    if dom is None:
        return "not defined"
    return DOMAIN_STATE_NAMES.get(dom.state(0)[0], "unknown")
