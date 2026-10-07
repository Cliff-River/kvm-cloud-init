"""libvirt 存储池卷操作：卷定义、流式上传、扩容与删除。

替代原 bash 流程中的 ``cp`` / ``qemu-img resize`` / ``virsh vol-delete``。
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET

import libvirt

from ..errors import ProvisionError

_UPLOAD_CHUNK = 8 * 1024 * 1024


def volume_xml(name: str, capacity_bytes: int, fmt: str) -> str:
    """生成 dir 存储池的 file 卷定义 XML。"""
    return (
        f"<volume type='file'>\n"
        f"  <name>{name}</name>\n"
        f"  <capacity unit='bytes'>{capacity_bytes}</capacity>\n"
        f"  <target><format type='{fmt}'/></target>\n"
        f"</volume>"
    )


def lookup_volume(pool: libvirt.virStoragePool, name: str) -> libvirt.virStorageVol | None:
    try:
        return pool.storageVolLookupByName(name)
    except libvirt.libvirtError:
        return None


def delete_volume(pool: libvirt.virStoragePool, name: str) -> bool:
    """删除池中指定卷，不存在则返回 False。"""
    vol = lookup_volume(pool, name)
    if vol is None:
        return False
    vol.delete(0)
    return True


def upload_file(
    pool: libvirt.virStoragePool,
    name: str,
    local_path: str,
    fmt: str,
    capacity_bytes: int,
) -> libvirt.virStorageVol:
    """在池中创建卷并通过 libvirt stream 上传本地文件内容。"""
    if not os.path.isfile(local_path):
        raise ProvisionError(f"待上传文件不存在：{local_path}")
    conn = pool.connect()
    vol = pool.createXML(volume_xml(name, capacity_bytes, fmt), 0)
    stream = conn.newStream(0)
    file_size = os.path.getsize(local_path)
    try:
        vol.upload(stream, 0, file_size, 0)
        with open(local_path, "rb") as fh:
            while True:
                buf = fh.read(_UPLOAD_CHUNK)
                if not buf:
                    break
                while buf:
                    sent = stream.send(buf)
                    buf = buf[sent:]
        stream.finish()
    except Exception:
        stream.abort()
        try:
            vol.delete(0)
        except libvirt.libvirtError:
            pass
        raise
    return vol


def resize_volume(vol: libvirt.virStorageVol, capacity_bytes: int) -> None:
    """把卷扩容到目标虚拟大小（字节，绝对值，只增不减）。"""
    # 本绑定中 virStorageVolInfo 是列表：[state, capacity, allocation]
    current = vol.info()[1]
    if capacity_bytes <= current:
        return
    vol.resize(capacity_bytes, 0)


def volume_path(vol: libvirt.virStorageVol) -> str:
    root = ET.fromstring(vol.XMLDesc(0))
    target = root.find("./target/path")
    if target is None or not target.text:
        raise ProvisionError("存储卷 XML 中缺少 target/path")
    return target.text


def delete_path(
    conn: libvirt.virConnect, pool: libvirt.virStoragePool, path: str
) -> str | None:
    """删除池目录下的残留文件。

    依次尝试：libvirt 卷删除 -> 本地文件删除 -> sudo rm 兜底。
    返回删除方式（"volume" / "file" / "sudo"），文件不存在返回 None。
    """
    if not os.path.exists(path):
        return None
    try:
        vol = conn.storageVolLookupByPath(path)
        vol.delete(0)
        return "volume"
    except libvirt.libvirtError:
        pass
    try:
        os.remove(path)
        return "file"
    except FileNotFoundError:
        return None
    except PermissionError:
        # -n：不交互弹密码；sudo 不可用时直接报错而不是挂起
        result = subprocess.run(
            ["sudo", "-n", "rm", "-f", "--", path],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ProvisionError(
                f"无法删除残留文件 {path}：libvirt 卷删除与本地删除均失败，"
                f"且 sudo rm 不可用（{result.stderr.strip() or '需要密码'}）"
            )
        return "sudo"
