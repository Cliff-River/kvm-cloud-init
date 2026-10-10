# KVM Cloud-init Factory · KVM Cloud-init 工厂

**[English](#english) · [中文](#中文)**

> 通过定义模板利用云镜像，快速创建、管理和销毁 KVM/libvirt 虚拟机，
> 支持多模板、多实例；基于 cloud-init NoCloud，
> 使用 Python（libvirt-python + pycdlib）实现。
>
> Define templates to rapidly create, manage, and destroy KVM/libvirt VMs
> from cloud images, with multi-template, multi-instance support. Driven by
> cloud-init NoCloud, implemented in Python (libvirt-python + pycdlib).

---

<a id="english"></a>
# English

## Overview

`kvm-cloud-init` provisions KVM virtual machines from cloud qcow2 images
without an interactive installer. OS templates and instances are defined in
YAML files; a single command renders the cloud-init seed, uploads storage, and
starts the domain.

All operations go through the system-level **`qemu:///system`** connection via
libvirt API calls — there is no shelling out to `virsh` / `virt-install` /
`genisoimage` / `qemu-img`.

## Requirements

- Linux host with KVM, `libvirtd` running, and the `default` storage pool /
  NAT network active.
- Current user in the **libvirt** and **kvm** groups (log out and back in
  after adding).
- **System build prerequisites (required):** `pkg-config` and the libvirt
  development headers must be installed beforehand — `uv sync` compiles
  `libvirt-python` from source and fails without them:

  ```bash
  # Debian / Ubuntu
  sudo apt install -y pkg-config libvirt-dev
  # Rocky / RHEL
  sudo dnf install -y pkgconfig libvirt-devel
  ```

  Verify with `pkg-config --modversion libvirt` (should print a version).
- OVMF firmware for UEFI guests (optional — the tool falls back to BIOS).
- [uv](https://docs.astral.sh/uv/) and Python 3.13+.
- qcow2 cloud images placed under `images/` (configurable).

Install dependencies:

```bash
uv sync
```

## Quick Start

```bash
# 1. Review/edit the config files and templates/<name>/{meta-data,network-config,user-data}
# 2. List available templates
uv run kvm-cloud-init templates
# 3. Create an instance defined in instances.conf (idempotent)
uv run kvm-cloud-init create rocky-llm
# 4. Inspect
uv run kvm-cloud-init list
# 5. Destroy
uv run kvm-cloud-init destroy rocky-llm
```

## CLI

| Command | Description |
| --- | --- |
| `create <name> [--template T] [--yes] [--force]` | Create the instance; a same-named domain is destroyed first. `--template` overrides the instance's template reference, or allows creating an unregistered instance. `--yes` skips the `production` confirmation; `--force` forces removal of a `production` same-named instance (equivalent to `--yes` for that check). Neither works on `protected`. |
| `destroy <name> [--force] [--yes]` | Graceful shutdown (waits up to `shutdown_timeout` seconds), delete snapshots, undefine with NVRAM and all storage. `--force` powers off on timeout; `--yes` skips the `production` confirmation. `protected` instances are always rejected. |
| `list` | Configured instances plus any unregistered libvirt domains, with live state. |
| `templates` | List templates from `templates.conf`. |

## Deletion Protection

The `level` field (`normal` / `production` / `protected`, default `normal`)
can be set in any of the three config files with the usual precedence
(instance > template > `default.conf`). Unregistered instances are treated as
`normal`.

| Level | `destroy` | `create` with a same-named domain |
| --- | --- | --- |
| `normal` | Delete immediately | Implicitly destroy, then recreate |
| `production` | Interactive prompt; type `yes` to proceed, or pass `--yes` | Prompt, `--yes`, or `--force` to remove the old instance and recreate |
| `protected` | Rejected; edit the config (remove `level` or set it back to `normal`) and retry | Rejected, same remedy |

`protected` can never be bypassed with `--yes` / `--force` — editing the
config file is the only way out. On a non-TTY (CI / piped EOF) the
`production` prompt safely fails as "not confirmed" with an explicit error;
use `--yes` in automation. Note that `--force` has independent meanings: on
`destroy` it powers the VM off after the shutdown timeout, on `create` it
only overrides the `production` confirmation (the implicit destroy still
shuts down gracefully).

## Configuration

All three `.conf` files are YAML. Precedence:
**instance > template > `default.conf`**.

### `default.conf`

`storage_pool`, `image_dir`, `memory` (MiB), `vcpus`, `network`,
`firmware` (`auto`/`uefi`/`bios`), `graphics` (`vnc`/`spice`/`none`),
`video` (`auto`/`virtio`/`bochs`/`cirrus`/`qxl`),
`level` (`normal`/`production`/`protected`, implicit default `normal`),
`shutdown_timeout` (seconds).
`graphics: none` leaves the serial console only; `video: auto` omits the
`<video>` element so libvirt/qemu picks its default model. See
[Deletion Protection](#deletion-protection) for `level` semantics.

### `templates.conf`

```yaml
rocky:                                # template name, top-level key
  path: templates/Rocky-LVM           # directory with the three cloud-init files
  image_dir: images/                  # optional, defaults to default.conf image_dir
  image: Rocky-10-GenericCloud.qcow2  # required
  os_variant: rocky10                 # recorded in domain metadata
  memory: 6144                        # optional overrides
  vcpus: 6
  capacity: 100G                      # optional disk target size
  graphics: spice                     # optional: vnc/spice/none
  video: virtio                       # optional: auto/virtio/bochs/cirrus/qxl
  level: production                   # optional: normal/production/protected
  user-data: |                        # optional inline override, per file
    #cloud-config
    ...
```

The top level of `templates.conf` is the template table itself — there is no
wrapping `templates:` key (the file name already provides that namespace).

Each inline `meta-data` / `network-config` / `user-data` block replaces the
same-named file under `path`; omitted files are loaded from the directory.

### `instances.conf`

```yaml
rocky-dev:              # instance name (= libvirt domain name), top-level key
  template: rocky
  memory: 8192
  vcpus: 4
  capacity: 100G
  graphics: none        # optional: headless (serial only)
  level: protected      # optional: normal/production/protected
  user-data: |          # whole-document replacement, no YAML deep merge
    #cloud-config
    ...
```

The instance name is the libvirt domain name. Each instance gets its own
storage volumes `<name>.qcow2` and `<name>-cidata.iso`, so multiple instances
can coexist — give them distinct hostnames/IPs in their own `network-config`
/ `meta-data`.

## How It Works

1. Config is loaded and merged; cloud-init documents are rendered
   (instance inline > template inline > template directory).
2. A temporary directory holds the rendered files and a `cidata.iso` built with
   pycdlib (volume label `cidata`, RockRidge + Joliet), and is removed
   afterwards.
3. The qcow2 image and seed ISO are uploaded to the storage pool over libvirt
   streams; `capacity` is applied via the storage volume resize API.
4. The domain XML is built directly (virtio disk/NIC, SATA CD-ROM, VNC/serial
   console, host-passthrough CPU) and the persistent domain is defined and
   started. UEFI uses libvirt firmware auto-selection with BIOS fallback.
5. Destroying shuts the domain down gracefully, deletes snapshots, undefines
   with NVRAM, and removes external-snapshot overlays, memory-state files and
   backing-chain base images via structured XML parsing.

## Tests

```bash
uv run pytest
```

Tests mock libvirt/pycdlib where appropriate and need no root or KVM.

## Troubleshooting

- **Cannot connect to qemu:///system** — start `libvirtd` and ensure your user
  belongs to `libvirt`/`kvm`; log out and back in.
- **Storage pool/network missing** — `sudo virsh net-start default`,
  `sudo virsh net-autostart default`; ensure the `default` pool exists and is
  running.
- **VM boots BIOS despite UEFI** — install OVMF (`ovmf` / `edk2-ovmf`).
- **Shutdown timeout** — re-run with `--force`, or investigate the guest.
- **Plain-text passwords** — the bundled `user-data` files contain lab
  passwords; replace them or switch to SSH keys before exposing a VM.

## Migration from the Bash Version

The old `install.sh` / `undefine.sh` were removed. Behavior differences:

- Per-instance volume names replace the single fixed image path; multiple
  instances are now supported.
- Disk `capacity` moved from a CLI flag to template/instance configuration.
- No more external CLI dependencies beyond libvirtd itself.

---

<a id="中文"></a>
# 中文

## 简介

`kvm-cloud-init` 用云 qcow2 镜像在 KVM 上交付虚拟机，无需交互式安装。
OS 模板与实例通过 YAML 文件定义，一条命令即可渲染 cloud-init 种子、上传
存储并启动域。

所有操作均通过系统级 **`qemu:///system`** 连接调用 libvirt API 完成，
不再 shell 调用 `virsh` / `virt-install` / `genisoimage` / `qemu-img`。

## 环境要求

- Linux 主机：开启 KVM，`libvirtd` 运行中，`default` 存储池与 NAT 网络活动。
- 当前用户属于 **libvirt** 与 **kvm** 组（加入后需重新登录）。
- **系统级编译前置（必需）**：需预先安装 `pkg-config` 与 libvirt 开发头文件，
  否则 `uv sync` 从源码编译 `libvirt-python` 时会失败：

  ```bash
  # Debian / Ubuntu
  sudo apt install -y pkg-config libvirt-dev
  # Rocky / RHEL
  sudo dnf install -y pkgconfig libvirt-devel
  ```

  可用 `pkg-config --modversion libvirt` 验证（应输出版本号）。
- UEFI 客户机需要 OVMF 固件（可选，缺失时回退 BIOS）。
- 已安装 [uv](https://docs.astral.sh/uv/)，Python 3.13+。
- qcow2 云镜像放在 `images/`（可配置）。

安装依赖：

```bash
uv sync
```

## 快速开始

```bash
# 1. 检查/编辑配置文件与 templates/<名称>/ 下的 cloud-init 三件套
# 2. 查看可用模板
uv run kvm-cloud-init templates
# 3. 创建 instances.conf 中定义的实例（幂等，可重复执行）
uv run kvm-cloud-init create rocky-llm
# 4. 查看状态
uv run kvm-cloud-init list
# 5. 销毁
uv run kvm-cloud-init destroy rocky-llm
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `create <名称> [--template T] [--yes] [--force]` | 创建实例，同名域会先被销毁；`--template` 可覆盖实例引用的模板，也可对未登记实例临时指定模板；`--yes` 跳过 production 确认；`--force` 强制删除 production 同名旧实例后重建（对该检查等价于 `--yes`）；二者对 protected 均无效 |
| `destroy <名称> [--force] [--yes]` | 优雅关机（等待 `shutdown_timeout` 秒）、删除快照、带 NVRAM undefine 并清理全部存储；`--force` 超时后强制断电；`--yes` 跳过 production 确认；protected 实例始终被拒绝 |
| `list` | 列出已登记实例与未登记的 libvirt 域及实时状态 |
| `templates` | 列出 `templates.conf` 中定义的模板 |

## 删除保护

`level` 字段取值 `normal` / `production` / `protected`，缺省为 `normal`，
可在三份配置文件的任意一处设置，仍按
**实例 > 模板 > `default.conf`** 合并；未登记实例按 `normal` 处理。

| 级别 | `destroy` | `create` 检测到同名域 |
| --- | --- | --- |
| `normal` | 直接删除 | 隐式销毁后重建 |
| `production` | 交互提示，输入 `yes` 才继续，或加 `--yes` 跳过 | 交互确认，或加 `--yes` / `--force` 删除旧实例后重建 |
| `protected` | 直接拒绝；需改配置（删除 `level` 或改回 `normal`）后重试 | 同样直接拒绝，解除方式相同 |

`protected` 在任何情况下都无法被 `--yes` / `--force` 绕过，唯一解除方式是
修改配置文件。非 TTY 环境（CI / 管道 EOF）下 production 的确认会安全失败，
按未确认处理并给出明确错误，自动化场景请显式加 `--yes`。注意两个
`--force` 语义相互独立：destroy 的 `--force` 是关机超时后强制断电，
create 的 `--force` 只用于绕过 production 确认（隐式销毁仍走优雅关机）。

## 配置说明

三个 `.conf` 均为 YAML。优先级：
**实例 > 模板 > `default.conf`**。

### `default.conf`

字段：`storage_pool`、`image_dir`、`memory`（MiB）、`vcpus`、`network`、
`firmware`（`auto`/`uefi`/`bios`）、`graphics`（`vnc`/`spice`/`none`）、
`video`（`auto`/`virtio`/`bochs`/`cirrus`/`qxl`）、
`level`（`normal`/`production`/`protected`，隐式缺省 `normal`）、
`shutdown_timeout`（秒）。
`graphics: none` 表示无图形设备、仅保留串口控制台；`video: auto` 表示不写
`<video>` 元素，由 libvirt/qemu 采用默认显卡型号。`level` 的行为见
[删除保护](#删除保护)。

### `templates.conf`

```yaml
rocky:                                # 模板名，顶层键
  path: templates/Rocky-LVM           # 内含 cloud-init 三件套的目录
  image_dir: images/                  # 可选，缺省取 default.conf
  image: Rocky-10-GenericCloud.qcow2  # 必填
  os_variant: rocky10                 # 记入域 metadata
  memory: 6144                        # 以下均为可选覆盖
  vcpus: 6
  capacity: 100G                      # 磁盘目标大小
  graphics: spice                     # 可选：vnc/spice/none
  video: virtio                       # 可选：auto/virtio/bochs/cirrus/qxl
  level: production                   # 可选：normal/production/protected
  user-data: |                        # 可选内联覆盖，按文件粒度
    #cloud-config
    ...
```

`templates.conf` 的顶层就是模板表本身，没有 `templates:` 包装键
（文件名已经提供了这层命名空间）。

内联 `meta-data` / `network-config` / `user-data` 会整块替换 `path` 目录中的
同名文件；未内联的文件仍从目录读取。

### `instances.conf`

```yaml
rocky-dev:              # 实例名（即 libvirt 域名），顶层键
  template: rocky
  memory: 8192
  vcpus: 4
  capacity: 100G
  graphics: none        # 可选：无图形，仅串口控制台
  level: protected      # 可选：normal/production/protected
  user-data: |          # 整块替换，不做 YAML 深合并
    #cloud-config
    ...
```

实例名即 libvirt 域名。每个实例使用独立的存储卷 `<名称>.qcow2` 与
`<名称>-cidata.iso`，因此可同时存在多台实例——请在各自的 `network-config`
/ `meta-data` 中区分主机名和 IP。

## 工作原理

1. 加载并合并配置，渲染 cloud-init 三份文档（实例内联 > 模板内联 > 模板目录）。
2. 在临时目录中用 pycdlib 生成卷标 `cidata`（RockRidge + Joliet）的
   `cidata.iso`，流程结束后临时目录自动删除。
3. qcow2 镜像与种子 ISO 通过 libvirt stream 上传到存储池；`capacity` 通过
   存储卷扩容 API 生效。
4. 直接构建 domain XML（virtio 磁盘/网卡、SATA 光驱、VNC/串口控制台、
   host-passthrough CPU），定义持久域并启动；UEFI 走 libvirt 固件自动选择，
   缺失时回退 BIOS。
5. 销毁时优雅关机、删除快照、带 NVRAM undefine，并通过结构化 XML 解析清理
   外部快照 overlay、内存状态文件及 backing chain 上的基础镜像。

## 测试

```bash
uv run pytest
```

测试对 libvirt/pycdlib 做了必要的 mock，不需要 root 或真实 KVM。

## 常见问题

- **无法连接 qemu:///system**：启动 `libvirtd`，确认用户在 `libvirt`/`kvm`
  组，并重新登录。
- **缺少存储池/网络**：`sudo virsh net-start default`、
  `sudo virsh net-autostart default`，并确认 `default` 存储池存在且运行。
- **本应 UEFI 却以 BIOS 启动**：安装 OVMF（`ovmf` / `edk2-ovmf`）。
- **关机超时**：加 `--force` 重试，或排查客户机。
- **明文密码**：仓库自带 `user-data` 含实验密码，接入不可信网络前请替换或
  改用 SSH 公钥。

## 从 Bash 版本迁移

旧的 `install.sh` / `undefine.sh` 已删除。行为差异：

- 存储卷按实例命名，取代固定镜像路径，原生支持多实例；
- 磁盘 `capacity` 从命令行参数移至模板/实例配置；
- 除 libvirtd 外不再依赖任何外部命令行工具。

## Related Links · 相关链接

- cloud-init NoCloud: <https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html>
- libvirt: <https://libvirt.org/>
- pycdlib: <https://clalancette.github.io/pycdlib/>
