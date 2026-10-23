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
- **System build prerequisites (required):** the compiler toolchain
  (`build-essential`), `pkg-config`, and the libvirt development headers must
  be installed beforehand — `uv sync` compiles `libvirt-python` from source
  and fails without them:

  ```bash
  # Debian / Ubuntu
  sudo apt install -y build-essential pkg-config libvirt-dev
  # Rocky / RHEL
  sudo dnf install -y gcc make pkgconfig libvirt-devel
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
uv run kvmci templates
# 3. Create an instance defined in instances.conf (idempotent, waits for cloud-init)
uv run kvmci create rocky-llm
# 4. Inspect
uv run kvmci list
uv run kvmci status rocky-llm
uv run kvmci cloud-init status rocky-llm
# 5. Start / stop / reboot lifecycle
uv run kvmci stop rocky-llm
uv run kvmci start rocky-llm
uv run kvmci reboot rocky-llm
# 6. SSH into the instance (key + real IP are resolved automatically)
uv run kvmci shell rocky-llm
# 7. Run a one-off command
uv run kvmci exec rocky-llm -- uname -a
# 8. Destroy (also removes the auto-generated key pair)
uv run kvmci destroy rocky-llm
```

## CLI

| Command | Description |
| --- | --- |
| `create <name> [--template T] [--yes] [--force] [--no-wait] [--verbose]` | Create the instance; a same-named domain is destroyed first. Waits for cloud-init by default (`--no-wait` returns immediately; `--verbose` shows a rolling 7-line view). `--template` overrides the instance's template reference, or allows creating an unregistered instance. `--yes` skips the `production` confirmation; `--force` forces removal of a `production` same-named instance (equivalent to `--yes` for that check). Neither works on `protected`. Also generates `keys/<name>` + `keys/<name>.pub` and injects the public key into the rendered `user-data` at runtime. |
| `destroy <name> [--force] [--yes]` | Graceful shutdown (waits up to `shutdown_timeout` seconds), delete snapshots, undefine with NVRAM and all storage, then delete the auto-generated key pair `keys/<name>` + `keys/<name>.pub`. `--force` powers off on timeout; `--yes` skips the `production` confirmation. `protected` instances are always rejected (and their keys are kept). |
| `start <name>` | Start a shut-off instance (`dom.create`). Already running is a no-op with `already running` and exit 0. |
| `stop <name> [--force]` | Graceful ACPI shutdown, polling for up to `shutdown_timeout` seconds. `--force` does **not** skip the graceful attempt; it only powers the guest off after the timeout (same as `destroy --force`). Does not undefine or delete storage/keys. |
| `reboot <name>` | ACPI reboot of a running instance; a shut-off guest is an error (exit 1). |
| `status <name>` | Read-only `Name` / `Template` / `State` / `vCPUs` / `Memory` / `IPv4`; `IPv4` falls back to `-` when it cannot be resolved. |
| `cloud-init status <name>` | Run `cloud-init status --long`. Exit code: `0` done, `2` running / not started, `1` error or query failure. |
| `cloud-init log <name> [-f / --follow]` | Print `/var/log/cloud-init.log`; `-f` streams it until cloud-init reports done (`0`) or error (`1`). |
| `list` | Configured instances plus any unregistered libvirt domains, with live state. |
| `templates` | List templates from `templates.conf`. |
| `shell <name>` | Open an interactive SSH session. Starts the instance first if it is shut off, then waits for an IPv4 address. Uses the instance's private key and the **actual** IPv4 address discovered from libvirt (DHCP lease → qemu-guest-agent → ARP), never the address declared in `network-config`. |
| `exec <name> -- <command>` | Run one command non-interactively over SSH; starts the instance first if it is shut off. Everything after `--` is joined and passed to the remote shell; stdout/stderr are streamed and the remote exit status becomes the `kvmci` exit code. |

### Waiting for cloud-init

`create` waits for cloud-init after the domain starts. IP/SSH is retried every
2 seconds for up to 300 seconds; the status is read with
`cloud-init status --format=json` (parsed with `json.loads`). On success it
prints `Cloud-init completed successfully in <n>s.` and exits `0`. On failure
it prints `Cloud-init failed:` plus the `--long` summary and the last 20 lines
of `/var/log/cloud-init-output.log`, then exits `1` (the instance keeps
running). `Ctrl+C` stops waiting, prints
`Stopped waiting for cloud-init. Instance <name> keeps running.`, and exits
`0`.

While waiting it prints `Waiting for cloud-init to complete...`, then
`Connected to <name> at <ip>.` once SSH is reachable. On a TTY it shows a
spinner (`⠋⠙⠹…`) refreshed in place every 0.1s, including an
`SSH: waiting for connection` phase before the IP is available. When output is
redirected it appends a status line every 15 seconds instead. `--verbose` also
streams the last 3 lines of `cloud-init-output.log` plus the last 3 lines of
the debug `/var/log/cloud-init.log` (which grows continuously). Cloud-init logs
are usually root/group-only, so reads automatically fall back to `sudo -n`; the
SSH user therefore needs passwordless sudo to see them.

Exit codes are `0` success, `1` business failure, `2` usage error.

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
2. An Ed25519 (falling back to RSA-3072) key pair is generated under
   `keys/<name>`, and the public key is appended to the rendered `user-data`
   for the first non-root user (adding a sudo user `cliff` when none exists);
   the login user is recorded in the domain metadata. Existing user-defined
   keys are never overwritten.
3. A temporary directory holds the rendered files and a `cidata.iso` built with
   pycdlib (volume label `cidata`, RockRidge + Joliet), and is removed
   afterwards.
4. The qcow2 image and seed ISO are uploaded to the storage pool over libvirt
   streams; `capacity` is applied via the storage volume resize API.
5. The domain XML is built directly (virtio disk/NIC, SATA CD-ROM, VNC/serial
   console, host-passthrough CPU) and the persistent domain is defined and
   started. UEFI explicitly uses the plain (non-Secure-Boot) OVMF pair
   resolved from QEMU firmware descriptors, with BIOS fallback when OVMF is
   unavailable.
6. `shell` / `exec` resolve the actual IPv4 address from libvirt
   (`virsh domifaddr`-equivalent, lease → agent → ARP), connect with the
   per-instance private key, and run via paramiko (no `ssh` subprocess).
7. Destroying shuts the domain down gracefully, deletes snapshots, undefines
   with NVRAM, removes external-snapshot overlays, memory-state files and
   backing-chain base images via structured XML parsing, and deletes the
   auto-generated key pair.

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
- **系统级编译前置（必需）**：需预先安装编译工具链（`build-essential`）、
  `pkg-config` 与 libvirt 开发头文件，否则 `uv sync` 从源码编译
  `libvirt-python` 时会失败：

``` bash
# Debian / Ubuntu
sudo apt install -y build-essential pkg-config libvirt-dev
# Rocky / RHEL
sudo dnf install -y gcc make pkgconfig libvirt-devel
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
uv run kvmci templates
# 3. 创建 instances.conf 中定义的实例（幂等，默认等待 cloud-init）
uv run kvmci create rocky-llm
# 4. 查看状态
uv run kvmci list
uv run kvmci status rocky-llm
uv run kvmci cloud-init status rocky-llm
# 5. 启停与重启
uv run kvmci stop rocky-llm
uv run kvmci start rocky-llm
uv run kvmci reboot rocky-llm
# 6. SSH 登录实例（自动使用密钥与实际 IP）
uv run kvmci shell rocky-llm
# 7. 执行一次性命令
uv run kvmci exec rocky-llm -- uname -a
# 8. 销毁（同时删除自动生成的密钥对）
uv run kvmci destroy rocky-llm
```

## 命令

| 命令 | 说明 |
| --- | --- |
| `create <名称> [--template T] [--yes] [--force] [--no-wait] [--verbose]` | 创建实例，同名域会先被销毁；默认等待 cloud-init 完成（`--no-wait` 立即返回，`--verbose` 以 7 行滚动区域展示输出）；`--template` 可覆盖实例引用的模板，也可对未登记实例临时指定模板；`--yes` 跳过 production 确认；`--force` 强制删除 production 同名旧实例后重建（对该检查等价于 `--yes`）；二者对 protected 均无效。同时生成 `keys/<名称>` 与 `keys/<名称>.pub`，并在运行时把公钥注入渲染后的 `user-data` |
| `destroy <名称> [--force] [--yes]` | 优雅关机（等待 `shutdown_timeout` 秒）、删除快照、带 NVRAM undefine 并清理全部存储，随后删除自动生成的密钥对 `keys/<名称>` 与 `keys/<名称>.pub`；`--force` 超时后强制断电；`--yes` 跳过 production 确认；protected 实例始终被拒绝（密钥一并保留） |
| `start <名称>` | 启动已关机的实例（`dom.create`）；已运行时提示 `already running` 并以 0 退出 |
| `stop <名称> [--force]` | ACPI 优雅关机，按 `shutdown_timeout` 轮询；`--force` **不会**跳过优雅关机，仅在超时后强制断电（与 `destroy --force` 语义一致）；不 undefine、不删除存储与密钥 |
| `reboot <名称>` | 对运行中的实例发送 ACPI reboot；已关机时报错退出 1 |
| `status <名称>` | 只读输出 `Name` / `Template` / `State` / `vCPUs` / `Memory` / `IPv4`；IPv4 解析失败时显示 `-` |
| `cloud-init status <名称>` | 执行 `cloud-init status --long`；退出码：done 为 0，running / not started 为 2，error 或查询失败为 1 |
| `cloud-init log <名称> [-f / --follow]` | 输出 `/var/log/cloud-init.log`；`-f` 实时跟随到 cloud-init done（0）或 error（1） |
| `list` | 列出已登记实例与未登记的 libvirt 域及实时状态 |
| `templates` | 列出 `templates.conf` 中定义的模板 |
| `shell <名称>` | 打开 SSH 交互式登录会话；实例已关机时先自动启动并等待 IPv4 就绪。使用实例私钥与 libvirt 实测 IPv4 地址（DHCP 租约 → qemu-guest-agent → ARP 依次回退），绝不信任 `network-config` 中声明的地址 |
| `exec <名称> -- <命令>` | 非交互执行一条命令；实例已关机时先自动启动并等待 IPv4 就绪。`--` 之后的所有参数拼接后交给远端 shell，实时透传 stdout/stderr，远端退出码即 `kvmci` 退出码 |

### 等待 cloud-init

`create` 在域启动后默认等待 cloud-init：IP/SSH 每 2 秒重试，最多 300 秒；
通过 `cloud-init status --format=json` 读取状态（用 `json.loads` 解析）。
成功时打印 `Cloud-init completed successfully in <n>s.` 并退出 0；失败时向
stderr 打印 `Cloud-init failed:`、`--long` 摘要与
`/var/log/cloud-init-output.log` 末尾 20 行，退出 1（实例保持运行）。
`Ctrl+C` 会停止等待并打印
`Stopped waiting for cloud-init. Instance <名称> keeps running.`，退出 0。

等待期间会先打印 `Waiting for cloud-init to complete...`，SSH 可达后打印
`Connected to <名称> at <ip>.`。TTY 下用 `⠋⠙⠹…` 转圈动画在同一行原地刷新
（每 0.1 秒），IP 未就绪时显示 `SSH: waiting for connection`，连上后显示
`Cloud-init status: running (15s elapsed)`；输出被重定向时不做动画，
每 15 秒逐行追加一次状态。`--verbose` 还会追踪 `cloud-init-output.log`
末尾 3 行与持续增长的调试日志 `/var/log/cloud-init.log` 末尾 3 行。
cloud-init 日志通常仅 root/组可读，读取失败时会自动回退到 `sudo -n`，
因此 SSH 用户需要免密 sudo 才能看到日志。

退出码约定：`0` 成功，`1` 业务失败，`2` 参数错误。

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
2. 在 `keys/<名称>` 下生成密钥对（首选 Ed25519，回退 RSA-3072），把公钥追加到
   渲染后的 `user-data` 中第一个非 root 用户（若不存在则新增 sudo 用户 `cliff`），
   并把登录用户记入域元数据；用户自定义的公钥绝不会被覆盖。
3. 在临时目录中用 pycdlib 生成卷标 `cidata`（RockRidge + Joliet）的
   `cidata.iso`，流程结束后临时目录自动删除。
4. qcow2 镜像与种子 ISO 通过 libvirt stream 上传到存储池；`capacity` 通过
   存储卷扩容 API 生效。
5. 直接构建 domain XML（virtio 磁盘/网卡、SATA 光驱、VNC/串口控制台、
   host-passthrough CPU），定义持久域并启动；UEFI 显式使用从 QEMU 固件
   描述符解析出的无安全启动普通 OVMF（CODE + VARS 模板），缺失 OVMF 时
   回退 BIOS。
6. `shell` / `exec` 从 libvirt 实测 IPv4（等价 `virsh domifaddr`，按
   租约 → guest-agent → ARP 回退），用实例私钥经 paramiko 连接
   （不调用 `ssh` 子进程）。
7. 销毁时优雅关机、删除快照、带 NVRAM undefine，并通过结构化 XML 解析清理
   外部快照 overlay、内存状态文件及 backing chain 上的基础镜像，同时删除
   自动生成的密钥对。

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
