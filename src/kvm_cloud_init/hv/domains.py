"""域生命周期：优雅关机、快照/残留文件收集、undefine。

本模块完整迁移原 ``undefine.sh`` 的行为，但全部使用 libvirt API 与
结构化 XML 解析（不再使用 virsh 与正则）。
"""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import libvirt

from .. import qcow2
from ..errors import ShutdownTimeout
from . import storage
from .client import pool_target_path

# undefine 标志位：托管保存状态 / 快照元数据 / NVRAM / 检查点元数据 / TPM 状态
_UNDEFINE_FLAGS = (
    libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
    | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
    | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
    | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA
    | getattr(libvirt, "VIR_DOMAIN_UNDEFINE_TPM", 0)
)

# 关机信号重发策略：运行态实例每隔 SHUTDOWN_RETRY_INTERVAL 秒重发一次
# ACPI 关机，最多发送 SHUTDOWN_MAX_ATTEMPTS 次（含首次）；次数用尽后仅在
# 允许强制断电（--force）时直接 power off，否则继续等到 timeout 再报错。
SHUTDOWN_RETRY_INTERVAL = 60
SHUTDOWN_MAX_ATTEMPTS = 3


def lookup_domain(conn: libvirt.virConnect, name: str) -> libvirt.virDomain | None:
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError:
        return None


def domain_disk_files(xml_desc: str) -> set[str]:
    """从域 XML 中提取磁盘/光驱引用的源文件。"""
    root = ET.fromstring(xml_desc)
    return {
        element.get("file")
        for element in root.findall("./devices/disk/source[@file]")
        if element.get("file")
    }


def snapshot_files(xml_desc: str) -> set[str]:
    """从快照 XML 中提取外部磁盘 overlay 与内存状态文件。"""
    root = ET.fromstring(xml_desc)
    files: set[str] = set()
    for element in root.findall(".//source[@file]"):
        if element.get("file"):
            files.add(element.get("file"))
    for element in root.findall(".//memory[@file]"):
        files.add(element.get("file"))
    return files


def _under_pool(path: str, pool_path: str) -> bool:
    """仅收集存储池目录下的绝对路径，避免误删其他文件。"""
    try:
        return os.path.commonpath([os.path.abspath(path), pool_path]) == pool_path
    except ValueError:
        return False


def collect_leftover_files(dom: libvirt.virDomain, pool_path: str) -> list[str]:
    """收集 undefine 后需要手动清理的文件。

    来源：域当前引用的磁盘/光驱、各快照 XML 中的 overlay/内存文件，
    以及沿 qcow2 backing file 链追出的基础镜像。
    """
    files: set[str] = domain_disk_files(dom.XMLDesc(0))
    for name in dom.snapshotListNames(0):
        snap = dom.snapshotLookupByName(name, 0)
        files |= snapshot_files(snap.getXMLDesc(0))

    # 沿 backing chain 扩展（迭代处理新加进来的节点）
    pending = list(files)
    while pending:
        current = pending.pop()
        try:
            info = qcow2.read_info(current)
        except (OSError, ValueError):
            continue
        if not info.backing_file:
            continue
        parent = info.backing_file
        if not os.path.isabs(parent):
            parent = os.path.join(os.path.dirname(os.path.abspath(current)), parent)
        parent = os.path.abspath(parent)
        if parent not in files:
            files.add(parent)
            pending.append(parent)

    return sorted(path for path in files if _under_pool(path, pool_path))


def _delete_all_snapshots(dom: libvirt.virDomain) -> None:
    """先尝试连子快照一起删元数据；失败则回退为普通递归删除。"""
    for name in list(dom.snapshotListNames(0)):
        snap = dom.snapshotLookupByName(name, 0)
        try:
            snap.delete(
                libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_CHILDREN
                | libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY
            )
        except libvirt.libvirtError:
            snap.delete(libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_CHILDREN)


class _ShutdownProgress:
    """关机等待进度：TTY 下行内刷新，非 TTY 逐行输出。"""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._inline = sys.stdout.isatty() and log is print
        self._shown = False

    def report(self, message: str) -> None:
        if not self._inline:
            self._log(message)
            return
        sys.stdout.write(f"\r\x1b[2K{message}")
        sys.stdout.flush()
        self._shown = True

    def clear(self) -> None:
        if self._inline and self._shown:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            self._shown = False


def shutdown_and_wait(
    dom: libvirt.virDomain,
    timeout: int,
    force: bool = False,
    log: Callable[[str], None] = print,
    retry_interval: float = SHUTDOWN_RETRY_INTERVAL,
    max_attempts: int = SHUTDOWN_MAX_ATTEMPTS,
) -> None:
    """优雅关机并轮询到 shut off；超时按 force 决定强杀或报错。

    运行中的实例每隔 ``retry_interval`` 秒重发一次 ACPI 关机信号，最多
    发送 ``max_attempts`` 次；次数用尽后若 ``force`` 为真则直接强制断电，
    否则不再发信号，继续等到 ``timeout`` 后报错。
    """
    name = dom.name()
    progress = _ShutdownProgress(log)
    state = dom.state(0)[0]
    if state == libvirt.VIR_DOMAIN_SHUTOFF:
        log(f"Instance {name} is already shut off.")
        return

    now = time.monotonic()
    deadline = now + timeout
    attempts = 0
    next_signal = now + retry_interval
    if state in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_BLOCKED):
        log(f"Instance {name} is running, sending shutdown signal ...")
        dom.shutdown()
        attempts = 1

    last_tick = -1
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        state = dom.state(0)[0]
        if state == libvirt.VIR_DOMAIN_SHUTOFF:
            progress.clear()
            log(f"Instance {name} has shut down.")
            return
        elapsed = int(timeout - (deadline - now))
        # 每 5 秒更新一次等待进度
        if elapsed - last_tick >= 5:
            last_tick = elapsed
            progress.report(
                f"Waiting for instance {name} to shut down ... {elapsed}s elapsed"
            )
        resendable = state in (
            libvirt.VIR_DOMAIN_RUNNING,
            libvirt.VIR_DOMAIN_BLOCKED,
        )
        if resendable and now >= next_signal:
            if attempts < max_attempts:
                attempts += 1
                next_signal = now + retry_interval
                progress.clear()
                log(
                    f"Instance {name} is still running; resending shutdown signal "
                    f"(attempt {attempts} of {max_attempts}) ..."
                )
                dom.shutdown()
            elif force:
                progress.clear()
                log(
                    f"Instance {name} did not shut down after {attempts} shutdown "
                    f"signal(s); forcing power off."
                )
                dom.destroy()
                return
            else:
                # 信号次数已用尽且不允许强杀：不再重发，等 guest 自行完成关机
                next_signal = float("inf")
        time.sleep(1)

    progress.clear()
    if force:
        log(f"Instance {name} did not shut down within {timeout}s; forcing power off.")
        dom.destroy()
        return
    raise ShutdownTimeout(
        f"Instance {name} did not shut down within {timeout}s; "
        "retry with --force to power it off, or inspect the guest state with virsh"
    )


def stop_domain(
    conn: libvirt.virConnect,
    name: str,
    timeout: int,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> bool:
    """优雅关闭实例；域不存在时返回 False。

    ``stop`` 子命令与 ``destroy`` 共用这一套关机逻辑：
    ACPI shutdown -> 轮询 ``timeout`` 秒 -> 仅在 ``force`` 时强制断电。
    """
    dom = lookup_domain(conn, name)
    if dom is None:
        return False
    shutdown_and_wait(dom, timeout, force, log=log)
    return True


def destroy_domain(
    conn: libvirt.virConnect,
    name: str,
    pool_name: str,
    timeout: int,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> bool:
    """完整销毁流程。域不存在时返回 False，正常销毁返回 True。"""
    dom = lookup_domain(conn, name)
    if dom is None:
        return False

    pool = conn.storagePoolLookupByName(pool_name)
    if pool.info()[0] != libvirt.VIR_STORAGE_POOL_RUNNING:
        pool.create(0)

    shutdown_and_wait(dom, timeout, force, log=log)

    # 删快照元数据前必须先抓取快照 XML 中的外部文件
    leftovers = collect_leftover_files(dom, pool_target_path(pool))
    snap_count = len(dom.snapshotListNames(0))
    if snap_count:
        log(f"Deleting {snap_count} snapshot(s) ...")
    _delete_all_snapshots(dom)
    pool.refresh(0)

    log(f"Undefining domain {name} (NVRAM/snapshots/checkpoints) ...")
    undefine_warned = False
    try:
        dom.undefineFlags(_UNDEFINE_FLAGS)
    except libvirt.libvirtError:
        # 存在未受管存储等非致命问题时，域通常仍可 undefine；
        # 残留文件随后统一清理
        try:
            dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        except libvirt.libvirtError:
            undefine_warned = True
    pool.refresh(0)

    if leftovers:
        log(f"Cleaning up {len(leftovers)} leftover storage file(s) ...")
    for path in leftovers:
        storage.delete_path(conn, pool, path)

    if undefine_warned:
        # 不阻断流程，但提示用户核对（与原 bash 行为一致）
        log(
            f"Warning: undefine {name} reported non-fatal errors; "
            f"check 'virsh vol-list {pool_name}'"
        )
    return True
