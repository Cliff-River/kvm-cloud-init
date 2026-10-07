# AGENTS.md

面向 AI Agent 的项目导航。改动代码前先读本文件与
`.trae/rules/project_rules.md`。

## 项目是什么

`kvm-cloud-init` 把 cloud qcow2 镜像 + cloud-init NoCloud 种子自动化交付为
KVM 虚拟机，支持多 OS 模板与多实例。Python 3.13 + uv + src 布局。

## 目录结构

```
├── pyproject.toml              # uv 项目；console script: kvm-cloud-init
├── default.conf                # 全局默认值（YAML）
├── templates.conf              # OS 模板定义（YAML）
├── instances.conf              # 实例定义（YAML）
├── templates/<名称>/            # cloud-init 三件套：
│                               #   meta-data / network-config / user-data
├── images/                     # 源 qcow2 镜像（git-ignored）
├── src/kvm_cloud_init/
│   ├── __init__.py             # main() 入口
│   ├── cli.py                  # argparse: create/destroy/list/templates
│   ├── config.py               # 配置加载、校验、三级合并
│   ├── cloudinit.py            # cloud-init 文档渲染
│   ├── seediso.py              # pycdlib 生成 cidata.iso（临时目录）
│   ├── qcow2.py                # qcow2 头 / parse_size / backing chain
│   ├── provision.py            # 创建/销毁/查询编排
│   ├── errors.py               # KciError 体系（中文用户信息）
│   └── hv/                     # libvirt 交互子包（勿命名为 libvirt）
│       ├── client.py           # qemu:///system 连接、存储池
│       ├── firmware.py         # UEFI 检测（域能力 + 文件系统回退）
│       ├── storage.py          # 卷 XML / stream 上传 / 扩容 / 删除
│       ├── domains.py          # 关机、快照、残留收集、undefine
│       └── guest.py            # domain XML 生成 + define/start
└── tests/                      # pytest，全部 mock，不需要 root/KVM
```

## 工作方式

- 配置合并优先级：实例 > 模板 > default.conf；cloud-init 文档整块替换。
- 创建：渲染文档 → 临时目录生成 cidata.iso（pycdlib）→ stream 上传镜像与
  ISO 到存储池（按实例命名卷）→ 可选卷扩容 → 构建 domain XML → define + start。
- 销毁：优雅关机轮询 → 收集域/快照 XML 中外部文件并追 backing chain →
  删快照 → undefine（NVRAM/checkpoints/TPM 标志位）→ 清理残留卷与文件。
- 连接统一为 `qemu:///system`；用户需在 libvirt/kvm 组。

## 开发与验证

```bash
uv sync
uv run pytest
uv run kvm-cloud-init templates
uv run kvm-cloud-init list
```

改动后至少保证 `uv run pytest` 全绿；改了配置字段或 CLI 行为要同步
README（中英双语）、三个 .conf 示例与本文件。

## 禁止事项

- 不要重新引入 Bash 脚本或 subprocess 包装 virsh/virt-install/genisoimage/qemu-img。
- 不要用正则解析 XML/JSON；用 `xml.etree` 与结构化解析。
- 不要使用 `qemu:///session`。
- 不要在测试里连接真实 libvirt 或启动虚拟机。
- 不要对 cloud-init 文档做 YAML 深合并（整块替换是既定语义）。
- 不要发明新的配置文件位置或绕过 config.py 的校验。

## 技术备忘

- virtinst 未发布到 PyPI，创建域改为直接生成 XML + libvirt API（见 hv/guest.py）。
- libvirt-python 12 绑定：`pool.info()`/`vol.info()` 无 flags 参数，info 为 list。
- `hv/` 不叫 `libvirt/`，避免与第三方 libvirt 包同名。
