"""幂等创建实例：镜像上传、种子 ISO、domain 定义与启动。"""

from __future__ import annotations

from pathlib import Path

import libvirt

from .. import cloudinit, qcow2, seediso, ssh
from ..config import ConfigStore
from ..errors import ImageNotFound
from ..hv import client, domains, firmware, guest
from ..hv import storage as hv_storage
from ..hv.client import pool_target_path
from .cloudinit_wait import wait_cloud_init
from .guard import ensure_destroy_allowed

DISK_SUFFIX = ".qcow2"
SEED_SUFFIX = "-cidata.iso"


def _disk_name(instance_name: str) -> str:
    return f"{instance_name}{DISK_SUFFIX}"


def _seed_name(instance_name: str) -> str:
    return f"{instance_name}{SEED_SUFFIX}"


def create_instance(
    store: ConfigStore,
    conn: libvirt.virConnect,
    name: str,
    template_name: str | None = None,
    yes: bool = False,
    force: bool = False,
    no_wait: bool = False,
    verbose: bool = False,
) -> int:
    """幂等创建实例：同名域存在时先销毁再创建，默认等待 cloud-init。

    ``yes`` / ``force`` 均可跳过 production 级别的交互确认；
    protected 级别在任何情况下都拒绝隐式销毁。
    返回进程退出码：cloud-init 等待失败时为 1，否则为 0。
    """
    resolved = store.resolve(name, template_name)
    if not resolved.image_path.is_file():
        raise ImageNotFound(
            f"Source image does not exist: {resolved.image_path} "
            f"(image of template {resolved.template.name})"
        )

    # 同名实例先销毁（优雅关机，不强制；失败时提示用户）
    if domains.lookup_domain(conn, name) is not None:
        ensure_destroy_allowed(
            name,
            resolved.level,
            skip_confirm=yes or force,
            action="destroy the existing instance and recreate it",
        )
        print(f"Instance {name} already exists; destroying it first ...")
        domains.destroy_domain(
            conn,
            name,
            resolved.storage_pool,
            resolved.shutdown_timeout,
            force=False,
        )

    docs = cloudinit.render_docs(store, resolved)

    # 运行时注入：生成/复用实例密钥对，把公钥追加进合并后 user-data，
    # 并确定 SSH 登录用户（不写回模板）。
    ssh.ensure_keypair(name)
    public_key = ssh.read_public_key(name)
    docs["user-data"], ssh_user = cloudinit.inject_ssh_access(
        docs["user-data"], public_key
    )

    pool = client.get_pool(conn, resolved.storage_pool, refresh=True)
    pool_dir = pool_target_path(pool)

    # 销毁后仍可能残留同名卷或未注册文件（例如上次创建中途失败），先清掉
    hv_storage.delete_path(conn, pool, str(Path(pool_dir) / _disk_name(name)))
    hv_storage.delete_path(conn, pool, str(Path(pool_dir) / _seed_name(name)))

    image_info = qcow2.read_info(str(resolved.image_path))

    with seediso.temporary_seed(docs) as seed_path:
        print(
            f"Uploading image {resolved.image_path.name} "
            f"to storage pool {resolved.storage_pool} ..."
        )
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
                print(f"Resizing disk to {resolved.capacity} ...")
                hv_storage.resize_volume(disk_vol, target)

        print("Uploading cidata.iso ...")
        hv_storage.upload_file(
            pool,
            _seed_name(name),
            str(seed_path),
            "raw",
            seed_path.stat().st_size,
        )

    firmware_mode = firmware.choose_firmware(resolved.firmware, conn)
    uefi_pair = (
        firmware.resolve_plain_uefi_pair() if firmware_mode == "efi" else None
    )
    if firmware_mode == "efi":
        if uefi_pair is not None:
            print(
                f"Booting with UEFI (firmware {uefi_pair[0]}, secure boot disabled)."
            )
        else:
            print(
                "Booting with UEFI (no plain OVMF descriptor found; "
                "libvirt will choose automatically)."
            )
    else:
        print("OVMF not detected; booting with BIOS.")

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
        loader=uefi_pair[0] if uefi_pair is not None else None,
        nvram_template=uefi_pair[1] if uefi_pair is not None else None,
        ssh_user=ssh_user,
    )
    guest.define_and_start(conn, xml_desc)
    print(
        f"Instance {name} created and started (template {resolved.template.name})."
    )

    if no_wait:
        return 0
    return wait_cloud_init(conn, name, verbose=verbose)
