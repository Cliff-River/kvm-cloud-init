# rocky-init

**[English](#english) · [中文](#中文)**

> One-command provisioning of a Rocky Linux 10 virtual machine on KVM/libvirt,
> fully automated with cloud-init (NoCloud).
>
> 基于 KVM/libvirt，通过 cloud-init（NoCloud）一键自动化部署 Rocky Linux 10 虚拟机。

---

<a id="english"></a>
# English

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Default VM Specification](#default-vm-specification)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)
- [Contributing](#contributing)
- [License](#license)
- [Useful Links](#useful-links)

---

## Overview

`rocky-init` is a small collection of shell scripts and [cloud-init](https://cloudinit.readthedocs.io/)
configuration files for creating a ready-to-use **Rocky Linux 10** KVM virtual
machine from the official GenericCloud qcow2 image.

Instead of walking through an interactive installer, you run a single command.
The script builds a cloud-init **NoCloud** seed ISO (`cidata.iso`), copies the
cloud image into libvirt's storage pool, optionally enlarges the virtual disk,
and imports the virtual machine with `virt-install`. On its first boot the VM
configures its hostname, static IP address, users, passwords, packages, and
LVM/XFS root filesystem automatically.

This project is intended for **local labs, development, and testing** on a
Linux desktop or server running libvirt.

## Features

- **One-command deployment** — provision a complete VM with `bash install.sh`.
- **Idempotent re-runs** — an existing VM with the same name is gracefully shut
  down and undefined (including its snapshots, NVRAM, storage, and files left
  behind by external snapshots) before a fresh one is created.
- **UEFI by default** — boots with UEFI when libvirt can find an OVMF firmware
  descriptor, and automatically falls back to legacy BIOS when OVMF is missing.
- **cloud-init automation (NoCloud datasource)** — no interactive installation:
  - creates a sudo user (`cliff`) with passwordless `sudo`;
  - sets passwords for the user and `root`, enables SSH password authentication
    and root login;
  - installs additional packages (`bash-completion`, `vim`);
  - sets the hostname to `rocky10`.
- **Static networking** — the VM is reachable at a fixed address
  (`192.168.122.11`) on libvirt's `default` NAT network.
- **Optional disk expansion** — pass `--capacity 100G` to enlarge the qcow2
  image; on first boot cloud-init automatically grows the partition, LVM
  physical volume, logical volume, and XFS filesystem to fill the new size.
- **VirtIO throughout** — virtio disk and network drivers for best performance.
- **Generated ISO is git-ignored** — `cidata.iso` is rebuilt from the three
  source files on every run, so edits always take effect.

## How It Works

```
 meta-data ┐
 network-config ├──▶ genisoimage ──▶ cidata.iso (volume label: cidata) ──┐
 user-data  ┘                                                             │
                                                                          ▼
 ~/OS/Rocky-10-GenericCloud-LVM-*.qcow2 ──cp──▶ /var/lib/libvirt/images/  ├──▶ virt-install
                                                   │                       │   (--import)
                                          qemu-img resize?                  │
                                           (--capacity SIZE)               ▼
                                                                     Rocky Linux 10 VM
                                                                     cloud-init first boot:
                                                                     hostname / network /
                                                                     users / packages /
                                                                     LVM + XFS grow
```

1. `install.sh` first calls `undefine.sh`, which shuts down any existing VM
   with the target name (waiting up to 120 seconds), deletes its snapshot
   metadata, and runs `virsh undefine --nvram --remove-all-storage`. Files
   left behind by external snapshots (disk overlays, memory-state files, and
   backing-chain base images not managed by libvirt) are collected beforehand
   and removed afterwards.
2. `genisoimage` packs `meta-data`, `network-config`, and `user-data` into
   `cidata.iso` with the `cidata` volume label, which the cloud-init
   **NoCloud** datasource recognises at boot.
3. The GenericCloud qcow2 image and `cidata.iso` are copied into
   `/var/lib/libvirt/images/` (the default libvirt storage pool).
4. If `--capacity` is given, `qemu-img resize` enlarges the copied qcow2 image.
5. `virt-install --import` boots the image directly with the seed ISO attached
   as a SATA CD-ROM. UEFI is used whenever an OVMF firmware descriptor exists
   (libvirt selects OVMF automatically); otherwise the script falls back to
   legacy BIOS. cloud-init runs on first boot and applies all settings.

## Project Structure

```
rocky-init/
├── install.sh        # Main provisioning script (arguments + virt-install)
├── undefine.sh       # Gracefully shuts down, deletes snapshots and removes an existing VM
├── user-data         # cloud-init cloud-config: users, passwords, packages, runcmd
├── meta-data         # cloud-init instance-id and local hostname
├── network-config    # cloud-init network config version 2 (static IP)
├── cidata.iso        # Generated NoCloud seed ISO (git-ignored, do not edit)
└── .gitignore        # Ignores the generated cidata.iso
```

| File | Format | Purpose |
| --- | --- | --- |
| `install.sh` | Bash | Parses arguments, builds the seed ISO, copies/resizes the image, creates the VM |
| `undefine.sh` | Bash | Safely powers off, deletes snapshots, and undefines a VM together with its NVRAM, storage, and external-snapshot leftover files |
| `user-data` | cloud-config | User accounts, passwords, packages, first-boot commands |
| `meta-data` | YAML | `instance-id` and `local-hostname` |
| `network-config` | YAML v2 | Static IP, gateway, and DNS servers for the VM |
| `cidata.iso` | ISO 9660 | Generated cloud-init seed; safe to delete at any time |

## Requirements

### Host system

- A Linux host (Debian/Ubuntu, Fedora/Rocky Linux, etc.) with hardware
  virtualization enabled in the BIOS/UEFI (Intel VT-x or AMD-V).
- KVM + libvirt, with `libvirtd` running.
- The following command-line tools:
  - `virt-install` and `virsh` (libvirt clients),
  - `genisoimage` (or a compatible ISO generator),
  - `qemu-img` (QEMU disk utilities),
  - `sudo`.
- The libvirt **`default`** NAT network active (subnet `192.168.122.0/24`).
- (Optional, for UEFI boot) OVMF firmware. Without it `install.sh` prints a
  warning and falls back to legacy BIOS.
- The official Rocky Linux 10 GenericCloud qcow2 image downloaded to
  `~/OS/` (the filename must match `IMG_FILENAME` in `install.sh`).

### Installing dependencies

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install qemu-kvm libvirt-daemon-system libvirt-clients \
                 virtinst genisoimage qemu-utils
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm "$USER"   # log out and back in afterwards
```

Fedora / Rocky Linux (host):

```bash
sudo dnf install qemu-kvm libvirt virt-install genisoimage qemu-img
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt "$USER"       # log out and back in afterwards
```

Enable the default NAT network (only needed once):

```bash
sudo virsh net-start default
sudo virsh net-autostart default
virsh net-list --all
```

### Obtaining the cloud image

Download the **Rocky Linux 10 GenericCloud (LVM, x86_64)** qcow2 image from
the official Rocky Linux download site and place it in `~/OS/`:

```bash
mkdir -p ~/OS && cd ~/OS
wget https://download.rockylinux.org/pub/rocky/10/images/x86_64/\
Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2
```

If that exact build is no longer mirrored, pick the latest file of the same
type from <https://download.rockylinux.org/pub/rocky/10/images/x86_64/> and
either rename it or update `IMG_FILENAME` in
[install.sh](file:///home/cliff/OS/rocky-init/install.sh).

You can verify KVM support with:

```bash
egrep -c '(vmx|svm)' /proc/cpuinfo   # a non-zero result means virtualization is available
```

## Quick Start

```bash
cd rocky-init

# 1. (Optional) review and edit the cloud-init files
vim user-data network-config meta-data

# 2. Provision the VM with default settings
bash install.sh

# 3. Log in once cloud-init finishes (first boot takes about 1–3 minutes)
ssh cliff@192.168.122.11
```

## Usage

### `install.sh`

```text
bash install.sh [VM_NAME] [--capacity SIZE]
```

- `VM_NAME` — optional positional argument. The libvirt domain name for the new
  VM. Defaults to `Rocky-Linux` when omitted.
- `--capacity SIZE` — optionally enlarge the copied qcow2 virtual disk to
  `SIZE` before booting (for example `50G`, `100G`). The size is passed
  directly to `qemu-img resize`; values larger than the current disk size
  grow the disk. The partition/LVM/XFS expansion itself happens automatically
  on the VM's first boot via `user-data`.
- Unknown options and extra positional arguments are rejected with an error.

Examples:

```bash
# Default name "Rocky-Linux", original image size
bash install.sh

# Custom VM name
bash install.sh my-rocky

# Expand the virtual disk to 100 GiB
bash install.sh my-rocky --capacity 100G

# Default name with disk expansion
bash install.sh --capacity 100G
```

The console is not attached automatically (`--noautoconsole`). Watch progress
with:

```bash
virsh list --all
virsh console Rocky-Linux     # exit the console with Ctrl + ]
```

### `undefine.sh`

```text
bash undefine.sh [VM_NAME]
```

Shuts down an existing VM gracefully (`virsh shutdown`) and polls its state
for up to 120 seconds. It then:

1. collects the external-snapshot files referenced by the domain and snapshot
   XML — disk overlays, memory-state files — and follows each qcow2 backing
   file chain to include the base image;
2. deletes all snapshot metadata with `virsh snapshot-delete`;
3. runs `virsh undefine --nvram --remove-all-storage` (the `--nvram` flag is
   mandatory for UEFI VMs, otherwise libvirt refuses with
   `cannot undefine domain with nvram`);
4. removes any files not managed by the storage pool, using
   `virsh vol-delete` first and `sudo rm` as a fallback.

If the VM does not power off within 120 seconds the script aborts rather than
forcing destruction. Defaults to `Rocky-Linux` when no name is given.
`install.sh` invokes this script automatically, so it is normally used only
for manual cleanup.

### Logging in to the VM

| Method | Value |
| --- | --- |
| SSH | `ssh cliff@192.168.122.11` |
| User password | `sec202609` |
| Root password | `sec202609` |
| Serial console | `sudo virsh console Rocky-Linux` |

## Configuration

All customization is done by editing the plain-text source files; the seed ISO
is regenerated on every run, so no manual ISO rebuilding is required.

### `user-data` (cloud-config)

[user-data](file:///home/cliff/OS/rocky-init/user-data) currently configures:

- SSH password authentication enabled (`ssh_pwauth: true`);
- a user `cliff` in the `sudo` group with `/bin/bash`, an unlocked password,
  and passwordless sudo (`ALL=(ALL) NOPASSWD:ALL`);
- passwords for `root` and `cliff` via `chpasswd` (`expire: false`);
- root login enabled (`disable_root: false`);
- packages `bash-completion` and `vim`;
- first-boot commands that grow partition 4, the LVM physical volume, the
  `rocky/lvroot` logical volume, and the XFS filesystem:

  ```text
  growpart /dev/vda 4
  pvresize /dev/vda4
  lvextend -l +100%FREE /dev/rocky/lvroot
  xfs_growfs /dev/rocky/lvroot
  ```

Common edits: change the username/passwords, add entries under `packages`,
replace password login with SSH public keys
(`ssh_authorized_keys:`), or append commands to `runcmd`.

### `meta-data`

[meta-data](file:///home/cliff/OS/rocky-init/meta-data) sets:

- `instance-id: rocky-10-001`
- `local-hostname: rocky10`

### `network-config`

[network-config](file:///home/cliff/OS/rocky-init/network-config) uses cloud-init
network configuration format v2 on interface `enp1s0`:

| Setting | Value |
| --- | --- |
| IPv4 address | `192.168.122.11/24` |
| Gateway | `192.168.122.1` |
| DNS servers | `192.168.122.1`, `223.5.5.5` |
| DHCP | disabled |

Change the address here if `192.168.122.11` conflicts with another host, and
make sure the new address stays inside the libvirt `default` subnet.

### `install.sh` variables

The top of [install.sh](file:///home/cliff/OS/rocky-init/install.sh) defines:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VM_NAME` | `Rocky-Linux` | Default domain name |
| `CAPACITY` | _(empty)_ | Optional target disk size for `qemu-img resize` |
| `IMG_FILENAME` | `Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2` | Source image filename in `~/OS/` and in the pool |
| `IMG_LIBVIRT_DIR` | `/var/lib/libvirt/images` | libvirt storage pool directory |

VM resources are set directly on the `virt-install` command line and can be
edited there:

- `--memory 6144` — 6 GiB RAM,
- `--vcpus 6` — 6 vCPUs,
- `--boot uefi` — used when an OVMF firmware descriptor is found; the script
  automatically switches to `--boot bios` otherwise,
- `--os-variant rocky10` — OS variant for Rocky Linux 10.

> **Single-instance note:** every run copies the image to the same fixed path
> in `/var/lib/libvirt/images/`. The project is therefore designed around one
> VM at a time. To keep several VMs simultaneously, parameterize the image
> destination path per VM name.

## Default VM Specification

| Item | Value |
| --- | --- |
| Operating system | Rocky Linux 10 (GenericCloud, LVM layout) |
| libvirt name | `Rocky-Linux` (overridable via positional argument) |
| Hostname | `rocky10` |
| Memory | 6144 MiB |
| vCPUs | 6 |
| Disk bus / NIC model | virtio / virtio |
| Firmware | UEFI (OVMF auto-selected by libvirt); legacy BIOS fallback |
| Cloud-init datasource | NoCloud (`cidata.iso` on a SATA CD-ROM) |
| Network | static `192.168.122.11/24`, gateway `.1` |
| Default user | `cliff` (passwordless sudo) |
| OS variant | `rocky10` |

## Troubleshooting

- **`错误：未找到 genisoimage` / `genisoimage: command not found`**
  Install the generator (`sudo apt install genisoimage` or
  `sudo dnf install genisoimage`) and re-run.

- **`错误：未找到 qemu-img` / `qemu-img: command not found`**
  Install the QEMU utilities (`qemu-utils` on Debian/Ubuntu, `qemu-img` on
  Fedora/Rocky Linux). The check only runs when `--capacity` is used.

- **`cp: cannot stat .../Rocky-10-GenericCloud-*.qcow2`**
  The source image is missing from `~/OS/`. Download it (see
  [Obtaining the cloud image](#obtaining-the-cloud-image)) or correct
  `IMG_FILENAME`.

- **Permission denied for `virsh` / `virt-install`**
  Make sure `libvirtd` is running and your user belongs to the `libvirt`
  (and, where applicable, `kvm`) group; log out and back in after adding it.

- **VM has no network / cannot be reached at `192.168.122.11`**
  Confirm the default network is active with `virsh net-list --all`, start it
  with `sudo virsh net-start default`, and check for address conflicts. First
  boot takes 1–3 minutes while cloud-init and package installation run.

- **VM did not shut down within 120 seconds**
  `undefine.sh` aborts on purpose. Check `virsh domstate <name>`; if the guest
  is unresponsive, force it off with `virsh destroy <name>` and run the script
  again.

- **`cannot undefine domain with nvram`**
  The VM uses UEFI and libvirt requires an explicit `--nvram`. `undefine.sh`
  already passes it; if you run `virsh undefine` manually, add `--nvram`.

- **`Storage volume '...' is not managed by libvirt. Remove it manually.`**
  This file comes from an external snapshot (a disk overlay or memory-state
  file). `undefine.sh` now collects and removes such files automatically; if
  an earlier interrupted run left one behind, delete it with
  `virsh vol-delete <path>` (or `sudo rm <path>`) and verify with
  `virsh vol-list default`.

- **`警告：未找到 OVMF UEFI 固件，回退到 BIOS 启动` / VM boots in BIOS mode**
  Install OVMF (`ovmf` on Debian/Ubuntu, `edk2-ovmf` on Fedora/Rocky Linux)
  if you want UEFI; the warning itself is harmless and the VM continues with
  legacy BIOS.

- **Disk size did not change inside the guest**
  `--capacity` grows the qcow2 file; the partition/LVM/XFS expansion is
  performed by the `runcmd` entries in `user-data` on first boot. Check
  `cloud-init status --long`, `/dev/vda4`, and `df -h /` inside the guest.

- **`错误：未知选项` / `错误：多余的参数`**
  Only one positional VM name and the `--capacity SIZE` option are accepted;
  check your command syntax.

## Security Notes

- `user-data` currently stores **plain-text default passwords**
  (`sec202609`) for both `root` and `cliff`, and both password login and root
  login are enabled. These defaults are for an isolated local lab only —
  change them before exposing the VM to any untrusted network, and preferably
  switch to SSH public-key authentication (`ssh_authorized_keys` with
  `ssh_pwauth: false`).
- Do not commit real secrets into `user-data`; restrict file permissions as
  needed (`chmod 600 user-data`).
- The VM uses a fixed static IP; never run two copies of it with the same
  network configuration on the same libvirt network.

## Contributing

Contributions are welcome. Suggested workflow:

1. Create a topic branch for your change (`fix/...`, `feat/...`).
2. Test `bash install.sh` and `bash undefine.sh` end to end on a libvirt host.
3. Keep the English and Chinese sections of this README in sync if you change
   documented behavior.
4. Submit a patch or pull request describing the change and the Rocky Linux
   version it was tested with.

Please report bugs with the full command line, script output,
`virsh --version`, and the guest image filename used.

## License

This repository currently ships **without a `LICENSE` file**, which means all
rights are reserved by the project owner by default. If you plan to reuse or
redistribute it, please contact the maintainer to add an appropriate license
(such as MIT) first.

## Useful Links

- Rocky Linux official site: <https://rockylinux.org/>
- Rocky Linux downloads (cloud images):
  <https://download.rockylinux.org/pub/rocky/10/images/x86_64/>
- cloud-init documentation: <https://cloudinit.readthedocs.io/>
- cloud-init NoCloud datasource:
  <https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html>
- libvirt virtualization: <https://libvirt.org/>
- virt-install documentation:
  <https://www.libvirt.org/manpages/virt-install.html>

---

<a id="中文"></a>
# 中文

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [使用说明](#使用说明)
- [配置说明](#配置说明)
- [虚拟机默认规格](#虚拟机默认规格)
- [常见问题](#常见问题)
- [安全提示](#安全提示)
- [参与贡献](#参与贡献)
- [开源许可](#开源许可)
- [相关链接](#相关链接)

---

## 项目简介

`rocky-init` 是一组精简的 Shell 脚本与 [cloud-init](https://cloudinit.readthedocs.io/)
配置文件，用于基于官方 GenericCloud qcow2 镜像，在 KVM 上创建一台开箱即用的
**Rocky Linux 10** 虚拟机。

无需走交互式安装流程，只需执行一条命令：脚本会自动生成 cloud-init 的
**NoCloud** 种子 ISO（`cidata.iso`），将云镜像复制到 libvirt 存储池，按需扩容
虚拟磁盘，并通过 `virt-install` 导入虚拟机。虚拟机首次启动时会自动完成主机名、
静态 IP、用户、密码、软件包以及 LVM/XFS 根文件系统的全部配置。

本项目面向运行 libvirt 的 Linux 桌面或服务器，适用于**本地实验、开发与测试**
场景。

## 功能特性

- **一键部署**：执行 `bash install.sh` 即可完成整台虚拟机的交付。
- **可重复执行**：创建前会先将同名虚拟机正常关机并 undefine（同时删除快照、
  NVRAM、存储以及外部快照遗留的文件），随后创建全新实例。
- **默认 UEFI 启动**：libvirt 能找到 OVMF 固件描述符时使用 UEFI 启动，缺少
  OVMF 时自动回退到传统 BIOS。
- **cloud-init 自动化（NoCloud 数据源）**，全程无需交互：
  - 创建具备免密 `sudo` 权限的用户（`cliff`）；
  - 设置普通用户与 `root` 密码，开启 SSH 密码登录与 root 登录；
  - 自动安装额外软件包（`bash-completion`、`vim`）；
  - 将主机名设置为 `rocky10`。
- **静态网络**：虚拟机在 libvirt `default` NAT 网络中使用固定地址
  `192.168.122.11`，随时可连。
- **可选磁盘扩容**：通过 `--capacity 100G` 扩大 qcow2 镜像；首次启动时
  cloud-init 会自动扩展分区、LVM 物理卷、逻辑卷与 XFS 文件系统至新容量。
- **全链路 VirtIO**：磁盘与网卡均使用 virtio 驱动，性能更佳。
- **生成的 ISO 已被 git 忽略**：`cidata.iso` 每次运行都由三个源文件重新生成，
  修改配置必然生效。

## 工作原理

```
 meta-data ┐
 network-config ├──▶ genisoimage ──▶ cidata.iso（卷标 cidata）────────┐
 user-data  ┘                                                          │
                                                                       ▼
 ~/OS/Rocky-10-GenericCloud-LVM-*.qcow2 ──cp──▶ /var/lib/libvirt/images/ ──▶ virt-install
                                                   │                        （--import）
                                          qemu-img resize?                   │
                                         （--capacity 大小）                 ▼
                                                                      Rocky Linux 10 虚拟机
                                                                      首次启动 cloud-init：
                                                                      主机名 / 网络 /
                                                                      用户 / 软件包 /
                                                                      LVM + XFS 扩容
```

1. `install.sh` 首先调用 `undefine.sh`：若存在同名虚拟机，先优雅关机
   （最长等待 120 秒）、删除其快照元数据，再执行
   `virsh undefine --nvram --remove-all-storage`。外部快照遗留的文件
   （磁盘 overlay、内存状态文件，以及不受 libvirt 管理的 backing file 链
   基础镜像）会在关机后先登记、undefine 后统一删除。
2. `genisoimage` 将 `meta-data`、`network-config`、`user-data` 打包为卷标为
   `cidata` 的 `cidata.iso`，cloud-init 的 **NoCloud** 数据源在启动时会自动
   识别该光盘。
3. 将 GenericCloud qcow2 镜像与 `cidata.iso` 复制到 libvirt 默认存储池
   `/var/lib/libvirt/images/`。
4. 若指定了 `--capacity`，通过 `qemu-img resize` 扩容复制后的 qcow2 镜像。
5. `virt-install --import` 将种子 ISO 作为 SATA 光驱挂载并直接启动镜像。
   存在 OVMF 固件描述符时使用 UEFI（由 libvirt 自动选择 OVMF），否则脚本
   回退到传统 BIOS；cloud-init 在首次启动时应用全部配置。

## 项目结构

```
rocky-init/
├── install.sh        # 主部署脚本（参数解析 + virt-install）
├── undefine.sh       # 优雅关机、删除快照并删除已有虚拟机
├── user-data         # cloud-init cloud-config：用户、密码、软件包、runcmd
├── meta-data         # cloud-init 实例 ID 与本地主机名
├── network-config    # cloud-init 网络配置 v2（静态 IP）
├── cidata.iso        # 自动生成的 NoCloud 种子 ISO（已被 git 忽略，请勿手改）
└── .gitignore        # 忽略生成的 cidata.iso
```

| 文件 | 格式 | 用途 |
| --- | --- | --- |
| `install.sh` | Bash | 解析参数、生成种子 ISO、复制/扩容镜像、创建虚拟机 |
| `undefine.sh` | Bash | 安全关机、删除快照，并 undefine 虚拟机及其 NVRAM、存储和外部快照遗留文件 |
| `user-data` | cloud-config | 用户账户、密码、软件包、首次启动命令 |
| `meta-data` | YAML | `instance-id` 与 `local-hostname` |
| `network-config` | YAML v2 | 虚拟机的静态 IP、网关与 DNS |
| `cidata.iso` | ISO 9660 | 自动生成的 cloud-init 种子，可随时删除 |

## 环境要求

### 宿主机

- 安装 Linux 操作系统（Debian/Ubuntu、Fedora/Rocky Linux 等），并已在
  BIOS/UEFI 中开启硬件虚拟化（Intel VT-x 或 AMD-V）。
- 安装 KVM 与 libvirt，且 `libvirtd` 服务正在运行。
- 以下命令行工具：
  - `virt-install` 与 `virsh`（libvirt 客户端）；
  - `genisoimage`（或兼容的 ISO 生成工具）；
  - `qemu-img`（QEMU 磁盘工具）；
  - `sudo`。
- libvirt 的 **`default`** NAT 网络处于活动状态（网段
  `192.168.122.0/24`）。
- （可选，UEFI 启动需要）OVMF 固件。未安装时 `install.sh` 会给出警告并
  回退到传统 BIOS。
- 已下载官方 Rocky Linux 10 GenericCloud qcow2 镜像到 `~/OS/` 目录，
  且文件名与 `install.sh` 中的 `IMG_FILENAME` 一致。

### 安装依赖

Debian / Ubuntu：

```bash
sudo apt update
sudo apt install qemu-kvm libvirt-daemon-system libvirt-clients \
                 virtinst genisoimage qemu-utils
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm "$USER"   # 之后需注销并重新登录
```

Fedora / Rocky Linux（宿主机）：

```bash
sudo dnf install qemu-kvm libvirt virt-install genisoimage qemu-img
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt "$USER"       # 之后需注销并重新登录
```

启用默认 NAT 网络（仅需一次）：

```bash
sudo virsh net-start default
sudo virsh net-autostart default
virsh net-list --all
```

### 获取云镜像

从 Rocky Linux 官方站点下载 **Rocky Linux 10 GenericCloud（LVM、x86_64）**
qcow2 镜像，并放入 `~/OS/`：

```bash
mkdir -p ~/OS && cd ~/OS
wget https://download.rockylinux.org/pub/rocky/10/images/x86_64/\
Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2
```

如果该具体版本已不再提供镜像，可在
<https://download.rockylinux.org/pub/rocky/10/images/x86_64/> 选择同类型的最新
文件，重命名为目标文件名，或修改
[install.sh](file:///home/cliff/OS/rocky-init/install.sh) 中的 `IMG_FILENAME`。

可通过以下命令确认 KVM 支持：

```bash
egrep -c '(vmx|svm)' /proc/cpuinfo   # 返回非 0 即表示硬件虚拟化可用
```

## 快速开始

```bash
cd rocky-init

# 1.（可选）检查并编辑 cloud-init 配置
vim user-data network-config meta-data

# 2. 使用默认配置创建虚拟机
bash install.sh

# 3. 等待 cloud-init 完成（首次启动约需 1–3 分钟）后登录
ssh cliff@192.168.122.11
```

## 使用说明

### `install.sh`

```text
bash install.sh [虚拟机名称] [--capacity 大小]
```

- `虚拟机名称`：可选位置参数，即新虚拟机在 libvirt 中的域名称；省略时默认为
  `Rocky-Linux`。
- `--capacity 大小`：可选。启动前将复制后的 qcow2 虚拟磁盘扩大到指定容量
  （如 `50G`、`100G`），参数会原样传递给 `qemu-img resize`；大于当前磁盘
  容量的值即表示扩容。分区/LVM/XFS 的实际扩展由 `user-data` 在虚拟机首次
  启动时自动完成。
- 遇到未知选项或多余的位置参数时，脚本报错并退出。

示例：

```bash
# 默认名称 "Rocky-Linux"，保持镜像原始大小
bash install.sh

# 自定义虚拟机名称
bash install.sh my-rocky

# 将虚拟磁盘扩大到 100 GiB
bash install.sh my-rocky --capacity 100G

# 使用默认名称并扩容磁盘
bash install.sh --capacity 100G
```

脚本使用 `--noautoconsole`，不会自动连接控制台。可通过以下命令查看进度：

```bash
virsh list --all
virsh console Rocky-Linux     # 按 Ctrl + ] 退出控制台
```

### `undefine.sh`

```text
bash undefine.sh [虚拟机名称]
```

先优雅关闭虚拟机（`virsh shutdown`），并轮询状态最长 120 秒。随后依次：

1. 从域 XML 与快照 XML 中登记外部快照文件——磁盘 overlay、内存状态文件，
   并沿 qcow2 backing file 链追出基础镜像；
2. 通过 `virsh snapshot-delete` 删除全部快照元数据；
3. 执行 `virsh undefine --nvram --remove-all-storage`（UEFI 虚拟机必须带
   `--nvram`，否则 libvirt 会报 `cannot undefine domain with nvram`）；
4. 对不受存储池管理的遗留文件，优先用 `virsh vol-delete` 删除，失败时
   以 `sudo rm` 兜底。

若 120 秒内未能关机，脚本将中止而不会强制销毁。未传名称时默认为
`Rocky-Linux`。`install.sh` 会自动调用该脚本，通常只在手动清理时单独使用。

### 登录虚拟机

| 方式 | 值 |
| --- | --- |
| SSH | `ssh cliff@192.168.122.11` |
| 普通用户密码 | `sec202609` |
| root 密码 | `sec202609` |
| 串口控制台 | `sudo virsh console Rocky-Linux` |

## 配置说明

所有定制均通过编辑纯文本源文件完成；种子 ISO 每次运行都会重新生成，无需手动
制作 ISO。

### `user-data`（cloud-config）

[user-data](file:///home/cliff/OS/rocky-init/user-data) 当前包含以下配置：

- 开启 SSH 密码认证（`ssh_pwauth: true`）；
- 创建用户 `cliff`，加入 `sudo` 组，使用 `/bin/bash`，密码未锁定，并拥有
  免密 sudo 权限（`ALL=(ALL) NOPASSWD:ALL`）；
- 通过 `chpasswd` 设置 `root` 与 `cliff` 的密码（`expire: false`）；
- 允许 root 登录（`disable_root: false`）；
- 安装软件包 `bash-completion` 与 `vim`；
- 首次启动时扩展第 4 分区、LVM 物理卷、`rocky/lvroot` 逻辑卷及 XFS 文件
  系统，命令如下：

  ```text
  growpart /dev/vda 4
  pvresize /dev/vda4
  lvextend -l +100%FREE /dev/rocky/lvroot
  xfs_growfs /dev/rocky/lvroot
  ```

常见修改：更换用户名/密码、在 `packages` 下增加软件包、使用
`ssh_authorized_keys:` 改为 SSH 公钥登录，或在 `runcmd` 中追加命令。

### `meta-data`

[meta-data](file:///home/cliff/OS/rocky-init/meta-data) 设置：

- `instance-id: rocky-10-001`
- `local-hostname: rocky10`

### `network-config`

[network-config](file:///home/cliff/OS/rocky-init/network-config) 使用 cloud-init
网络配置 v2 格式，作用于网卡 `enp1s0`：

| 配置项 | 值 |
| --- | --- |
| IPv4 地址 | `192.168.122.11/24` |
| 网关 | `192.168.122.1` |
| DNS 服务器 | `192.168.122.1`、`223.5.5.5` |
| DHCP | 关闭 |

若 `192.168.122.11` 与其他主机冲突，请在此修改地址，并确保新地址仍位于
libvirt `default` 网络的网段内。

### `install.sh` 变量

[install.sh](file:///home/cliff/OS/rocky-init/install.sh) 开头定义了：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `VM_NAME` | `Rocky-Linux` | 默认虚拟机域名称 |
| `CAPACITY` | _（空）_ | 可选的目标磁盘大小，供 `qemu-img resize` 使用 |
| `IMG_FILENAME` | `Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2` | `~/OS/` 与存储池中的镜像文件名 |
| `IMG_LIBVIRT_DIR` | `/var/lib/libvirt/images` | libvirt 存储池目录 |

虚拟机资源直接在 `virt-install` 命令行中设置，可按需修改：

- `--memory 6144`：6 GiB 内存；
- `--vcpus 6`：6 个 vCPU；
- `--boot uefi`：找到 OVMF 固件描述符时使用；否则脚本自动改用
  `--boot bios`；
- `--os-variant rocky10`：Rocky Linux 10 对应的操作系统变体。

> **单实例说明**：每次运行都会把镜像复制到 `/var/lib/libvirt/images/` 下
> 同一个固定路径，因此本项目的设计是同一时间只运行一台虚拟机。若需同时保留
> 多台，请根据虚拟机名称将镜像目标路径参数化。

## 虚拟机默认规格

| 项目 | 值 |
| --- | --- |
| 操作系统 | Rocky Linux 10（GenericCloud，LVM 分区布局） |
| libvirt 名称 | `Rocky-Linux`（可通过位置参数覆盖） |
| 主机名 | `rocky10` |
| 内存 | 6144 MiB |
| vCPU | 6 |
| 磁盘总线 / 网卡型号 | virtio / virtio |
| 固件 | UEFI（由 libvirt 自动选择 OVMF），缺失时回退传统 BIOS |
| cloud-init 数据源 | NoCloud（SATA 光驱中的 `cidata.iso`） |
| 网络 | 静态地址 `192.168.122.11/24`，网关 `.1` |
| 默认用户 | `cliff`（免密 sudo） |
| 操作系统变体 | `rocky10` |

## 常见问题

- **提示 `错误：未找到 genisoimage`**
  请先安装该工具（`sudo apt install genisoimage` 或
  `sudo dnf install genisoimage`）后重新运行。

- **提示 `错误：未找到 qemu-img`**
  请安装 QEMU 磁盘工具（Debian/Ubuntu 为 `qemu-utils`，Fedora/Rocky Linux
  为 `qemu-img`）。该检查仅在使用 `--capacity` 时触发。

- **提示 `cp: cannot stat .../Rocky-10-GenericCloud-*.qcow2`**
  `~/OS/` 中缺少源镜像。请按[获取云镜像](#获取云镜像)下载，或修正
  `IMG_FILENAME`。

- **`virsh` / `virt-install` 权限被拒绝**
  请确认 `libvirtd` 已运行，且当前用户属于 `libvirt`（必要时还有 `kvm`）
  组；加入用户组后需注销并重新登录。

- **虚拟机没有网络 / 无法连接 `192.168.122.11`**
  使用 `virsh net-list --all` 确认 default 网络已活动，必要时执行
  `sudo virsh net-start default` 启动，并排查地址冲突。首次启动需要
  1–3 分钟运行 cloud-init 与安装软件包。

- **虚拟机 120 秒内未能关闭**
  `undefine.sh` 会因此主动中止。可通过 `virsh domstate <名称>` 检查状态；
  若客户机确实无响应，使用 `virsh destroy <名称>` 强制关机后重新运行脚本。

- **报错 `cannot undefine domain with nvram`**
  虚拟机使用 UEFI，libvirt 要求显式指定 `--nvram`。`undefine.sh` 已自带该
  参数；若手动执行 `virsh undefine`，请自行加上 `--nvram`。

- **报错 `Storage volume '...' is not managed by libvirt. Remove it manually.`**
  该文件来自外部快照（磁盘 overlay 或内存状态文件）。`undefine.sh` 现在会
  自动登记并删除这类文件；若之前中断的运行留下残留，可手动执行
  `virsh vol-delete <路径>`（或 `sudo rm <路径>`），再用
  `virsh vol-list default` 核对。

- **提示 `警告：未找到 OVMF UEFI 固件，回退到 BIOS 启动` / 虚拟机以 BIOS 启动**
  如需 UEFI，请安装 OVMF（Debian/Ubuntu 为 `ovmf`，Fedora/Rocky Linux 为
  `edk2-ovmf`）；该警告本身无害，虚拟机会继续以传统 BIOS 启动。

- **客户机内部磁盘容量未变化**
  `--capacity` 只扩大 qcow2 文件；分区/LVM/XFS 扩展由 `user-data` 中的
  `runcmd` 在首次启动时完成。可在客户机内检查
  `cloud-init status --long`、`/dev/vda4` 与 `df -h /`。

- **提示 `错误：未知选项` / `错误：多余的参数`**
  脚本只接受一个位置参数（虚拟机名称）与 `--capacity 大小` 选项，请检查
  命令格式。

## 安全提示

- `user-data` 当前以**明文形式保存默认密码**（`sec202609`，同时用于
  `root` 与 `cliff`），且密码登录与 root 登录均处于开启状态。该默认配置
  仅适用于隔离的本地实验环境：在将虚拟机接入任何不可信网络前，请务必修改
  密码，最好改用 SSH 公钥认证（配置 `ssh_authorized_keys` 并将
  `ssh_pwauth` 设为 `false`）。
- 请勿将真实密钥提交进 `user-data`；必要时收紧文件权限
  （`chmod 600 user-data`）。
- 虚拟机使用固定静态 IP，切勿在同一个 libvirt 网络中同时运行两份相同网络
  配置的副本。

## 参与贡献

欢迎提交贡献，建议流程如下：

1. 为修改创建主题分支（如 `fix/...`、`feat/...`）。
2. 在 libvirt 宿主机上完整测试 `bash install.sh` 与
   `bash undefine.sh`。
3. 若修改了文档中描述的行为，请同步更新本 README 的英文与中文部分。
4. 提交补丁或 Pull Request，并说明改动内容以及测试所用的 Rocky Linux
   版本。

提交缺陷报告时，请附上完整命令行、脚本输出、`virsh --version` 以及使用的
客户机镜像文件名。

## 开源许可

本仓库当前**未包含 `LICENSE` 文件**，这意味着默认情况下所有权利归项目所有者
保留。如需再次分发或复用，请先联系维护者添加合适的开源许可证（例如 MIT）。

## 相关链接

- Rocky Linux 官网：<https://rockylinux.org/>
- Rocky Linux 云镜像下载：
  <https://download.rockylinux.org/pub/rocky/10/images/x86_64/>
- cloud-init 文档：<https://cloudinit.readthedocs.io/>
- cloud-init NoCloud 数据源：
  <https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html>
- libvirt 虚拟化：<https://libvirt.org/>
- virt-install 手册：
  <https://www.libvirt.org/manpages/virt-install.html>
