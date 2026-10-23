# AGENTS.md

面向 AI Agent 的项目导航。改动代码前先读本文件与
`.trae/rules/project_rules.md`。

## 项目是什么

`kvm-cloud-init` 把 cloud qcow2 镜像 + cloud-init NoCloud 种子自动化交付为
KVM 虚拟机，支持多 OS 模板与多实例。Python 3.13 + uv + src 布局。

## 目录结构

```
├── pyproject.toml              # uv 项目；console script: kvmci
├── default.conf                # 全局默认值（YAML）
├── templates.conf              # OS 模板定义（YAML）
├── instances.conf              # 实例定义（YAML）
├── templates/<名称>/            # cloud-init 三件套：
│                               #   meta-data / network-config / user-data
├── images/                     # 源 qcow2 镜像（git-ignored）
├── keys/                       # 每实例 SSH 密钥对（git-ignored）：<名称> / <名称>.pub
├── src/kvm_cloud_init/
│   ├── __init__.py             # main() 入口
│   ├── cli.py                  # argparse: create/destroy/start/stop/reboot/status/
│   │                           #   list/templates/shell/exec/cloud-init
│   ├── config.py               # 配置加载、校验、三级合并
│   ├── cloudinit.py            # cloud-init 文档渲染 + 运行时公钥/用户注入
│   ├── seediso.py              # pycdlib 生成 cidata.iso（临时目录）
│   ├── qcow2.py                # qcow2 头 / parse_size / backing chain
│   ├── ssh.py                  # 密钥生成/复用/删除、paramiko 连接、shell/exec、
│   │                           #   capture_command / stream_command
│   ├── provision/              # 业务编排包（对外由 __init__.py 导出）
│   │   ├── __init__.py         # 统一导出公共 API
│   │   ├── guard.py            # 删除保护级别把关、production 交互确认
│   │   ├── create.py           # 创建/重建 + 默认等待 cloud-init
│   │   ├── lifecycle.py        # start/stop/reboot/status/destroy
│   │   ├── access.py           # SSH 目标解析与 shell/exec 编排
│   │   ├── listing.py          # list / templates
│   │   └── cloudinit_wait.py   # 等待 cloud-init、status、log [-f]
│   ├── errors.py               # KciError 体系（英文用户信息）
│   └── hv/                     # libvirt 交互子包（勿命名为 libvirt）
│       ├── client.py           # qemu:///system 连接、存储池、状态名
│       ├── firmware.py         # UEFI 检测 + 普通 OVMF CODE/VARS 解析（域能力/描述符/文件系统）
│       ├── storage.py          # 卷 XML / stream 上传 / 扩容 / 删除
│       ├── domains.py          # 共享关机（shutdown_and_wait/stop_domain）、快照、
│       │                       #   残留收集、undefine
│       ├── network.py          # 实测 IPv4：virDomainInterfaceAddresses（lease→agent→arp）
│       ├── osinfo.py           # os-variant -> libosinfo OS ID 解析（本机库/官方仓库）
│       └── guest.py            # domain XML 生成 + define/start + ssh-user 元数据读写
└── tests/                      # pytest，全部 mock，不需要 root/KVM
```

## 工作方式

- 配置合并优先级：实例 > 模板 > default.conf；cloud-init 文档整块替换。
- 删除保护字段 `level`（normal/production/protected，缺省 normal）同样三级合并；
  destroy 与 create 的隐式销毁在 `provision/guard.py` 统一把关：production 需交互输入
  yes（`--yes` 跳过；create 还可用 `--force`，但内部仍走优雅关机），非 TTY
  安全失败；protected 对任何标志都拒绝，只能改配置解除。未登记实例按 normal。
- 创建：生成/复用 `keys/<名称>` 密钥对 → 渲染文档并把公钥运行时注入合并后
  `user-data`（追加到首个非 root 用户，无则新增 `cliff`）→ 临时目录生成
  cidata.iso（pycdlib）→ stream 上传镜像与 ISO 到存储池（按实例命名卷）→
  可选卷扩容 → 构建 domain XML（含 `ssh-user` 元数据）→ define + start →
  默认等待 cloud-init（`--no-wait` 跳过，`--verbose` 7 行滚动）。
- 生命周期：`start` 用 `dom.create(0)`，`stop` 与 `destroy` 共用
  `hv/domains.shutdown_and_wait` / `stop_domain`（禁止第二套轮询）；
  `reboot` 用 `dom.reboot(0)`；`status` 只读打印六项键值。
- cloud-init：用 paramiko 执行 `cloud-init status --format=json`（`json.loads`），
  `log -f` 走 `ssh.stream_command` 跟随 `/var/log/cloud-init.log`。
- 销毁：优雅关机轮询 → 收集域/快照 XML 中外部文件并追 backing chain →
  删快照 → undefine（NVRAM/checkpoints/TPM 标志位）→ 清理残留卷与文件 →
  删除 `keys/<名称>` 与 `<名称>.pub`（保护级别未通过则不删）。
- shell/exec：从域 XML 读 `ssh-user` → libvirt 实测 IPv4
  （lease→agent→arp，见 hv/network.py）→ 用 `keys/<名称>` 私钥经 paramiko
  建立交互 shell 或执行命令；exit code 透传。
- 连接统一为 `qemu:///system`；用户需在 libvirt/kvm 组。
- 所有面向用户的输出（help、print、警告、错误、表头、状态名）均为英文。

## 开发与验证

```bash
uv sync
uv run pytest
uv run kvmci templates
uv run kvmci list
uv run kvmci create <实例名> [--no-wait] [--verbose]
uv run kvmci status <实例名>
uv run kvmci start|stop|reboot <实例名>
uv run kvmci cloud-init status <实例名>
uv run kvmci cloud-init log <实例名> [-f]
uv run kvmci shell <实例名>
uv run kvmci exec <实例名> -- <命令>
```

改动后至少保证 `uv run pytest` 全绿；改了配置字段或 CLI 行为要同步
README（中英双语）、三个 .conf 示例与本文件。

## 禁止事项

- 不要重新引入 Bash 脚本或 subprocess 包装 virsh/virt-install/genisoimage/qemu-img，
  也不要用 subprocess 调用 `ssh`/`ssh-keygen`（统一走 paramiko）。
- 不要用正则解析 XML/JSON；用 `xml.etree` 与结构化解析。
- 不要使用 `qemu:///session`。
- 不要在测试里连接真实 libvirt 或启动虚拟机，也不要做真实 SSH 连接。
- 不要对 cloud-init 文档做 YAML 深合并（整块替换是既定语义）。
- 不要发明新的配置文件位置或绕过 config.py 的校验。

## 技术备忘

- virtinst 未发布到 PyPI，创建域改为直接生成 XML + libvirt API（见 hv/guest.py）。
- libvirt-python 12 绑定：`pool.info()`/`vol.info()` 无 flags 参数，info 为 list。
- `hv/` 不叫 `libvirt/`，避免与第三方 libvirt 包同名。
- paramiko 5.x 的 `Ed25519Key` 无 `generate()`：`ssh._generate_key` 用
  `getattr(..., "generate", None)` 探测，缺失时回退 `RSAKey.generate(3072)`。
- `virDomainInterfaceAddresses` 支持 SRC_LEASE / SRC_AGENT / SRC_ARP 三类来源。
