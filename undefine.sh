#!/usr/bin/env bash
set -euo pipefail

VM_NAME="${1:-Rocky-Linux}"
IMG_DIR="/var/lib/libvirt/images"

# 先确认虚拟机是否存在（libvirtd 未运行时也会判定为不可操作）
if ! sudo virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
  echo "虚拟机 $VM_NAME 不存在，无需 undefine。"
  exit 0
fi

# 仅在虚拟机运行时才发送关机指令，避免 "domain is not running" 报错
if [[ "$(sudo virsh domstate "$VM_NAME" 2>/dev/null || true)" == "running" ]]; then
  sudo virsh shutdown "$VM_NAME"
fi

# 轮询确认虚拟机真正关机（最长等待 120 秒）
state=""
for ((i = 0; i < 120; i++)); do
  state=$(sudo virsh domstate "$VM_NAME" 2>/dev/null || true)
  [[ "$state" == "shut off" ]] && break
  sleep 1
done

if [[ "$state" != "shut off" ]]; then
  echo "错误：虚拟机 $VM_NAME 在 120 秒内未关闭（当前状态：${state:-未知}），已取消 undefine。" >&2
  echo "可执行 'sudo virsh domstate $VM_NAME' 检查；确认无响应后可用 'sudo virsh destroy $VM_NAME' 强制关机再重跑本脚本。" >&2
  exit 1
fi

# 外部快照（external snapshot）会产生独立的 overlay 磁盘文件和内存状态文件，
# snapshot-delete --metadata 只删元数据、undefine --remove-all-storage
# 也只管受 libvirt 存储池管理且被域引用的卷，这些文件需要先登记再手动清理。
declare -A leftover_files=()

add_leftover() {
  local f="${1:-}"
  # 仅收集镜像目录下的绝对路径，避免误删其他文件
  [[ "$f" == "$IMG_DIR"/* ]] && leftover_files["$f"]=1
  return 0
}

# 1) 域当前引用的磁盘/光驱源文件（外部快照后 vda 已指向 overlay）
while IFS= read -r f; do
  add_leftover "$f"
done < <(sudo virsh dumpxml "$VM_NAME" 2>/dev/null | grep -oP "(?<=<source )file='\K[^']+" || true)

# 2) 各快照 XML 中记录的外部文件（磁盘 overlay、内存状态文件），需在删元数据前抓取
snapshot_list=$(sudo virsh snapshot-list "$VM_NAME" --name 2>/dev/null | sed '/^$/d' || true)
if [[ -n "$snapshot_list" ]]; then
  while IFS= read -r snap; do
    [[ -z "$snap" ]] && continue
    while IFS= read -r f; do
      add_leftover "$f"
    done < <(sudo virsh snapshot-dumpxml "$VM_NAME" "$snap" 2>/dev/null | grep -oP "\bfile='\K[^']+" || true)
  done <<< "$snapshot_list"
fi

# 3) 沿 qcow2 backing file 链收集基础镜像（多层外部快照时尤为重要）
qemu_img_info() {
  qemu-img info --output=json "$1" 2>/dev/null || sudo qemu-img info --output=json "$1" 2>/dev/null || true
}
for img in "${!leftover_files[@]}"; do
  cur="$img"
  while [[ -n "$cur" ]]; do
    next=$(qemu_img_info "$cur" | grep -oP '"full-backing-filename"\s*:\s*"\K[^"]+' | head -1 || true)
    [[ -z "$next" || "$next" == "$cur" ]] && break
    add_leftover "$next"
    cur="$next"
  done
done

# 删除所有快照（先删子快照再删自身；外部快照仅删元数据，文件稍后统一清理）
if [[ -n "$snapshot_list" ]]; then
  echo "检测到快照，删除中 ..."
  while IFS= read -r snap; do
    [[ -z "$snap" ]] && continue
    sudo virsh snapshot-delete "$VM_NAME" "$snap" --metadata --children || \
      sudo virsh snapshot-delete "$VM_NAME" "$snap" --children
  done <<< "$snapshot_list"
fi

# 刷新存储池，让刚生成的 overlay 文件成为受管卷，undefine 才能顺带删除
sudo virsh pool-refresh default >/dev/null 2>&1 || true

echo "虚拟机 $VM_NAME 已关闭，执行 undefine ..."
# 存在未受管文件时 sudo virsh 会报错但域仍可 undefine，不能因此中止后续清理
undefine_failed=0
sudo virsh undefine "$VM_NAME" --nvram --remove-all-storage || undefine_failed=1

# 清理未受 libvirt 管理的快照残留文件
sudo virsh pool-refresh default >/dev/null 2>&1 || true
for f in "${!leftover_files[@]}"; do
  if sudo virsh vol-delete "$f" >/dev/null 2>&1; then
    echo "已删除快照残留卷：$f"
  elif [[ -e "$f" ]]; then
    # 存储池未托管但文件确实存在（如 root 拥有），用 sudo 兜底
    sudo rm -f "$f"
    echo "已删除快照残留文件：$f"
  fi
done

if (( undefine_failed )); then
  echo "注意：sudo virsh undefine 报告过非致命错误（通常为未受管存储卷），残留文件已尝试清理，可用 'sudo virsh vol-list default' 核对。" >&2
fi
