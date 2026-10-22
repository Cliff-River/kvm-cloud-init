# kvm-cloud-init 项目规则

## 项目定位

多模板 / 多实例 KVM 虚拟机管理工具。Python 3.13，src 布局，uv 管理依赖，
console script 入口：`kvmci`（Python 包名仍为 `kvm-cloud-init`）。

## 架构边界（不要越层）

- `cli.py`：只做 argparse 参数解析、表格输出与异常到退出码的转换，不写业务逻辑。
- `config.py`：三份 YAML 配置（default/templates/instances）的加载、校验、
  三级合并（实例 > 模板 > 默认值）。
- `cloudinit.py`：cloud-init 三份文档的渲染（实例内联 > 模板内联 > path 目录文件）；
  另含运行时把公钥注入合并后 `user-data`（追加到首个非 root 用户，无则新增 `cliff`）。
- `seediso.py`：临时目录 + pycdlib 生成卷标 `cidata` 的 ISO。
- `qcow2.py`：qcow2 头解析、容量单位、backing chain（纯标准库）。
- `ssh.py`：每实例密钥对（`keys/<实例名>`/`.pub`）的生成/复用/删除、
  paramiko 连接、交互 shell 与一次性 exec（不调用 `ssh`/`ssh-keygen` 子进程）。
- `hv/`：所有 libvirt 交互。`client`（连接/存储池）、`firmware`（UEFI 检测）、
  `storage`（卷上传/扩容/删除）、`domains`（关机/快照/undefine）、
  `network`（实测 IPv4：`virDomainInterfaceAddresses` lease→agent→arp）、
  `osinfo`（os-variant 到 libosinfo OS ID 的解析：本机 osinfo-db > 官方 GitLab 仓库 > 内置兜底）、
  `guest`（domain XML 生成与启动，含 `ssh-user` 元数据读写）。
- `provision.py`：编排以上模块完成 create/destroy/list/templates/shell/exec。
- `errors.py`：所有面向用户的异常，message 必须是可直接展示的中文。

## 硬性约定

1. 所有实例操作只连接 `qemu:///system`，禁止 `qemu:///session`。
2. 优先使用 Python 库（libvirt-python、pycdlib、PyYAML、标准库），
   不新增 subprocess 调用；`hv/storage.py` 的 `sudo -n rm` 是已论证的最后兜底。
3. 不解析 CLI 输出：libvirt XML 用 `xml.etree`，容量信息用 qcow2 头，
   禁止用正则抠 XML/JSON。
4. cloud-init 文档覆盖以整份文件为粒度，不做 YAML 深合并。
5. 全部代码加类型注解；新增配置字段必须同步更新校验逻辑、配置示例与 README。
6. 存储资源按实例命名：`<实例名>.qcow2`、`<实例名>-cidata.iso`。
7. 删除保护 `level`（normal/production/protected，缺省 normal）随三级合并；
   production 在 destroy/create 隐式销毁前需交互输入 yes（`--yes` 跳过，
   create 的 `--force` 等价跳过，内部仍优雅关机），非 TTY 必须安全失败；
   protected 对 `--yes`/`--force` 一律拒绝，只能改配置解除；把关逻辑在
   provision.py，拒绝时抛 errors.ProtectionDenied，未登记实例按 normal。
8. SSH 用 paramiko（Ed25519 优先，无 `generate` 时回退 RSA-3072），
   禁止 subprocess 调用 `ssh`/`ssh-keygen`；密钥存于 git-ignored 的
   `keys/<实例名>`(`.pub`)，私钥 0600、公钥 0644，连接只用程序私钥、
   不做密码回退；登录用户记录在 domain 元数据 `ssh-user`。
9. destroy 在保护级别校验通过后同步删除 `keys/<实例名>` 与 `.pub`
   （缺失跳过，删除失败仅告警不阻断）；create 的隐式销毁不删密钥。

## 常用命令

```bash
uv sync                  # 安装依赖
uv run pytest            # 单元测试（无需 root/KVM）
uv run kvmci templates
uv run kvmci list
uv run kvmci create <实例名> [--template 模板] [--yes] [--force] [--no-wait]
uv run kvmci shell <实例名>
uv run kvmci exec <实例名> -- <命令>
uv run kvmci destroy <实例名> [--force] [--yes]
```

## 测试要求

- 新增业务逻辑必须配 pytest 用例，测试放在 `tests/`。
- 禁止在单测中建立真实 libvirt 连接或启动虚机；libvirt 对象用 fake/mock，
  XML 用字符串 fixture。
- 涉及删除文件/存储卷、undefine、关机的改动，必须有对应测试覆盖。

## 注意事项

- 三个 `.conf` 文件名虽为 .conf，但内容是 YAML。
- libvirt-python 的部分绑定不接受 flags 参数（如 `pool.info()`、`vol.info()`），
  返回的 info 是 list 而非具名元组，改动时先核对绑定签名。
- 静默 libvirt 默认错误日志的注册在 `hv/client.py`，业务需自行 try/except。
