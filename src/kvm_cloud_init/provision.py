"""创建 / 销毁 / 查询实例的编排层。

把配置解析、cloud-init 渲染、临时 cidata.iso、libvirt 存储与域操作
按原 bash 流程顺序串起来。
"""

from __future__ import annotations

import sys
from pathlib import Path

import libvirt

from . import cloudinit, qcow2, seediso
from .config import ConfigStore, ResolvedInstance
from .errors import ImageNotFound, ProtectionDenied
from .hv import client, domains, firmware, guest
from .hv import storage as hv_storage
from .hv.client import DOMAIN_STATE_NAMES, pool_target_path

DISK_SUFFIX = ".qcow2"
SEED_SUFFIX = "-cidata.iso"


def _disk_name(instance_name: str) -> str:
    return f"{instance_name}{DISK_SUFFIX}"


def _seed_name(instance_name: str) -> str:
    return f"{instance_name}{SEED_SUFFIX}"


def _prompt_yes(prompt: str) -> bool:
    """production 级别的交互确认：读取一行输入，仅精确的 ``yes`` 视为确认。

    非 TTY 环境（CI / 管道 EOF）安全失败，返回 False，避免脚本误删。
    """
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


def _ensure_destroy_allowed(
    name: str, level: str, *, skip_confirm: bool, action: str
) -> None:
    """销毁 / 隐式销毁前按删除保护级别把关。

    - normal：直接放行；
    - production：skip_confirm（--yes / create 的 --force）放行，
      否则交互输入 yes 才继续，未确认即抛 ProtectionDenied；
    - protected：任何标志都无法绕过，直接抛 ProtectionDenied。
    """
    if level == "normal":
        return
    if level == "protected":
        raise ProtectionDenied(
            f"实例 {name} 的保护级别为 protected，已拒绝{action}。"
            "该级别无法被 --yes/--force 绕过，"
            "请修改配置文件（删除 level 字段或改回 normal）后重试。"
        )
    if skip_confirm:
        return
    prompt = (
        f"实例 {name} 的保护级别为 production，{action}不可恢复。\n"
        f"请输入 yes 确认{action}："
    )
    if _prompt_yes(prompt):
        return
    if sys.stdin.isatty():
        raise ProtectionDenied(f"未收到 yes 确认，已取消{action}。")
    raise ProtectionDenied(
        f"实例 {name} 的保护级别为 production，当前为非交互环境，{action}已中止；"
        "如确认执行请追加 --yes。"
    )


def create_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    template_name: str | None = None,
    yes: bool = False,
    force: bool = False,
) -> ResolvedInstance:
    """幂等创建实例：同名域存在时先销毁再创建。

    ``yes`` / ``force`` 均可跳过 production 级别的交互确认；
    protected 级别在任何情况下都拒绝隐式销毁。
    """
    resolved = store.resolve(name, template_name)
    if not resolved.image_path.is_file():
        raise ImageNotFound(
            f"源镜像不存在：{resolved.image_path}（模板 {resolved.template.name} 的 image）"
        )

    # 同名实例先销毁（优雅关机，不强制；失败时提示用户）
    if domains.lookup_domain(conn, name) is not None:
        _ensure_destroy_allowed(
            name,
            resolved.level,
            skip_confirm=yes or force,
            action="删除旧实例并重建",
        )
        print(f"检测到已存在的实例 {name}，先销毁 ...")
        domains.destroy_domain(
            conn,
            name,
            resolved.storage_pool,
            resolved.shutdown_timeout,
            force=False,
        )

    docs = cloudinit.render_docs(store, resolved)

    pool = client.get_pool(conn, resolved.storage_pool, refresh=True)
    pool_dir = pool_target_path(pool)

    # 销毁后仍可能残留同名卷或未注册文件（例如上次创建中途失败），先清掉
    hv_storage.delete_path(conn, pool, str(Path(pool_dir) / _disk_name(name)))
    hv_storage.delete_path(conn, pool, str(Path(pool_dir) / _seed_name(name)))

    image_info = qcow2.read_info(str(resolved.image_path))

    with seediso.temporary_seed(docs) as seed_path:
        print(f"上传镜像 {resolved.image_path.name} 到存储池 {resolved.storage_pool} ...")
        disk_vol = hv_storage.upload_file(
            pool,
            _disk_name(name),
            str(resolved.image_path),
            "qcow2",
            image_info.virtual_size,
        )
        if resolved.capacity:
            target = qcow2.parse_size(resolved.capacity)
            if target > image_info.virtual_size:
                print(f"扩容磁盘到 {resolved.capacity} ...")
                hv_storage.resize_volume(disk_vol, target)

        print("上传 cidata.iso ...")
        hv_storage.upload_file(
            pool,
            _seed_name(name),
            str(seed_path),
            "raw",
            seed_path.stat().st_size,
        )

    firmware_mode = firmware.choose_firmware(resolved.firmware, conn)
    if firmware_mode == "efi":
        print("使用 UEFI 启动。")
    else:
        print("未检测到 OVMF，使用 BIOS 启动。")

    xml_desc = guest.build_domain_xml(
        name=name,
        disk_path=str(Path(pool_dir) / _disk_name(name)),
        iso_path=str(Path(pool_dir) / _seed_name(name)),
        memory_mib=resolved.memory,
        vcpus=resolved.vcpus,
        network=resolved.network,
        firmware=firmware_mode,
        os_variant=resolved.template.os_variant,
        template_name=resolved.template.name,
        graphics=resolved.graphics,
        video=resolved.video,
    )
    guest.define_and_start(conn, xml_desc)
    print(f"实例 {name} 已创建并启动（模板 {resolved.template.name}）。")
    return resolved


def destroy_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> bool:
    """销毁实例。返回是否找到了同名域。

    ``yes`` 跳过 production 级别的交互确认；``force`` 仍是优雅关机超时后
    强制断电的既有语义；protected 级别一律拒绝。未登记实例按 normal 处理。
    """
    resolved = None
    if name in store.instances:
        resolved = store.resolve(name)
    level = resolved.level if resolved else "normal"
    _ensure_destroy_allowed(name, level, skip_confirm=yes, action="销毁")
    timeout = resolved.shutdown_timeout if resolved else store.defaults.shutdown_timeout
    pool_name = resolved.storage_pool if resolved else store.defaults.storage_pool

    existed = domains.destroy_domain(conn, name, pool_name, timeout, force)
    if not existed:
        print(f"实例 {name} 不存在，无需销毁。")
    else:
        print(f"实例 {name} 已销毁。")
    return existed


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
        return "未定义"
    return DOMAIN_STATE_NAMES.get(dom.state(0)[0], "unknown")
