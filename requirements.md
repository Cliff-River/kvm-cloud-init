# 需求：用 Python 重构 kvm-cloud-init

将现有 Bash 脚本重构为 Python 项目，实现多 OS 模板、多实例的 KVM 虚拟机管理。uv 已初始化（Python 3.13、src 布局、入口 `kvm-cloud-init`），直接开发。

## 要求

1. 代码放在 `src/kvm_cloud_init/`，全部加类型注解；按职责拆分为多个模块（cli / config / cloudinit / seediso / libvirt / provision / errors），不要堆在单文件。
2. 依赖用 uv 管理：`uv add pyyaml libvirt-python virtinst pycdlib`、`uv add --dev pytest`。**优先使用 Python 库而非 subprocess 调 CLI**，不造轮子：libvirt 操作用 libvirt-python（`virsh`/`undefine` 等全部走 API；域/快照/卷 XML 用 `xml.etree` 解析），创建/导入虚拟机用 virtinst（virt-install 的底层库），生成 cidata.iso 用 pycdlib（替代 genisoimage），镜像扩容/上传优先用 libvirt storage API。仅在 Python 库确实无法覆盖时才保留 CLI 兜底。**所有实例操作一律连接 `qemu:///system` 会话（系统级），禁止使用 `qemu:///session`。**
3. 三个配置文件均为 **YAML 格式**：
   - `default.conf`：全局默认值（存储池 `/var/lib/libvirt/images`、镜像目录 `images/`、内存、vCPU、网络、固件 auto、关机超时 120s）。
   - `templates.conf`：**以原 `template.conf` 的现有配置（Rocky-LLM 模板）为结构示例和起点，后续模板在同一结构上扩展，不要另起设计**。原 INI 字段一一对应：`[Rocky-LLM]`→模板名、`template-path`→`path`（修正为实际目录 `templates/Rocky-LLM`）、`images-path`→`image_dir`、`image-name`→`image`；再在该结构上扩展 `os_variant`、`memory`、`vcpus`、`capacity`（磁盘目标大小，缺省不扩容）与内联 `meta-data`/`network-config`/`user-data`（按文件覆盖 path 中同名文件）字段。
   - `instances.conf`：以实例名为键（即 libvirt domain），通过 `template` 引用模板，可覆盖 `memory`/`vcpus`/`capacity` 及整块替换三份 cloud-init 文档（不做 YAML 深合并）。
   - 优先级：实例 > 模板 > 默认值。配置文件需带注释示例。
4. CLI（argparse 子命令）：
   - `create <实例名> [--template T]`，同名实例先销毁再创建（幂等）；
   - `destroy <实例名> [--force]`；
   - `list`（实例配置 + libvirt 状态）；
   - `templates`。
5. 创建流程：用 `tempfile` 建临时目录，写入渲染后的三份 cloud-init 文件，pycdlib 生成卷标 `cidata` 的 iso；通过 libvirt storage API 将镜像与 iso 建为存储池卷并按需扩容，用 virtinst 定义并启动域（virtio、SATA 光驱、不自动连控制台，OVMF 检测后 UEFI/BIOS 回退）；临时目录用上下文管理器保证清理。
6. 销毁流程从 `undefine.sh` 完整迁移为 libvirt API 调用：优雅关机轮询、删快照、undefine（含 NVRAM 与存储卷）、从域/快照 XML 收集外部快照残留文件并沿 backing chain 追基础镜像后删除。
7. pytest 测试放 `tests/`，mock libvirt/virtinst/pycdlib（不建立真实 qemu:///system 连接），无需 root/KVM，`uv run pytest` 可直接通过；重点测：配置合并优先级、cloud-init 渲染覆盖、残留文件与 backing chain 收集、CLI 参数解析。
8. 更新 `README.md`（保持中英双语）：配置字段说明、CLI 用法、迁移说明。
9. 生成 `.trae/rules/project_rules.md`（架构、约定、常用命令、测试要求）和根目录 `AGENTS.md`（项目导航、入口、禁止事项）。
10. 完成后删除旧 `install.sh`、`undefine.sh` 及 `templates/Ubuntu/` 下的脚本副本。

## 验收

`uv run kvm-cloud-init templates|list` 正常；`uv run pytest` 全绿；旧脚本清理干净；文档与新行为一致。
