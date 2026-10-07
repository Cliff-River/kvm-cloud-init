SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 解析参数：第一个位置参数为虚拟机名称（未指定时使用默认值），支持 --capacity 选项
VM_NAME="Rocky-Linux"
CAPACITY=""
NAME_SET=0
IMG_FILENAME="Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2"
IMG_LIBVIRT_DIR="/var/lib/libvirt/images"
IMG_LIBVIRT_PATH="$IMG_LIBVIRT_DIR/$IMG_FILENAME"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --capacity)
      if [[ -z "$2" ]]; then
        echo "错误：--capacity 需要一个参数（例如 --capacity 100G）。" >&2
        exit 1
      fi
      CAPACITY="$2"
      shift 2
      ;;
    -*)
      echo "错误：未知选项 $1（支持 --capacity 大小，例如 --capacity 100G）。" >&2
      exit 1
      ;;
    *)
      if (( NAME_SET )); then
        echo "错误：多余的参数 $1。" >&2
        exit 1
      fi
      VM_NAME="$1"
      NAME_SET=1
      shift
      ;;
  esac
done

bash "$SCRIPT_DIR/undefine.sh" "$VM_NAME"

# 用 genisoimage 从 cloud-init 数据文件重新生成 cidata.iso
if ! command -v genisoimage >/dev/null 2>&1; then
  echo "错误：未找到 genisoimage，请先安装（例如 sudo apt install genisoimage）。" >&2
  exit 1
fi
rm -f "$SCRIPT_DIR/cidata.iso"
genisoimage -output "$SCRIPT_DIR/cidata.iso" -volid cidata -joliet -rock \
  "$SCRIPT_DIR/meta-data" "$SCRIPT_DIR/network-config" "$SCRIPT_DIR/user-data"

sudo cp "$HOME/OS/$IMG_FILENAME" "$IMG_LIBVIRT_DIR/"
sudo cp "$SCRIPT_DIR/cidata.iso" "$IMG_LIBVIRT_DIR/"

# 如果指定了 --capacity，扩容复制后的 qcow2 镜像  
if [[ -n "$CAPACITY" ]]; then
  if ! command -v qemu-img >/dev/null 2>&1; then
    echo "错误：未找到 qemu-img，请先安装（例如 sudo apt install qemu-utils）。" >&2
    exit 1
  fi
  echo "正在将镜像扩容到 $CAPACITY ..."
  sudo qemu-img resize "$IMG_LIBVIRT_PATH" "$CAPACITY"
fi

# 优先使用 UEFI（由 libvirt 按固件描述符自动选择 OVMF，不硬编码路径）；
# 主机缺少 OVMF 固件时回退到传统 BIOS。
# 注意：不能用 "ls glob1 glob2" 整体判断——任一 glob 无匹配时 ls 退出码非 0，
# 即使另一个路径存在也会误判。
uefi_available() {
  # libvirt/qemu 实际使用的固件描述符（最可靠）
  local f
  for f in /usr/share/qemu/firmware/*.json /etc/qemu/firmware/*.json; do
    [[ -f "$f" ]] && grep -qi uefi "$f" && return 0
  done
  # 回退：直接查找各发行版常见的 OVMF 固件文件
  for f in \
    /usr/share/OVMF/OVMF_CODE*.fd \
    /usr/share/edk2/ovmf/OVMF_CODE*.fd \
    /usr/share/edk2-ovmf/x64/OVMF_CODE*.fd \
    /usr/share/qemu/ovmf-x86_64-code.bin; do
    [[ -e "$f" ]] && return 0
  done
  return 1
}

if uefi_available; then
  BOOT_OPTS="--boot uefi"
  echo "使用 UEFI 启动。"
else
  BOOT_OPTS="--boot bios"
  echo "警告：未找到 OVMF UEFI 固件，回退到 BIOS 启动。如需 UEFI，请安装 ovmf（Debian/Ubuntu）或 edk2-ovmf（RHEL/Rocky）。" >&2
fi

sudo virt-install \
  --name "$VM_NAME" \
  --os-variant rocky10 \
  $BOOT_OPTS \
  --memory 6144 \
  --vcpus 6 \
  --disk path="$IMG_LIBVIRT_PATH",format=qcow2,bus=virtio \
  --disk path="$IMG_LIBVIRT_DIR/cidata.iso",device=cdrom,bus=sata \
  --network default,model=virtio \
  --import \
  --noautoconsole

