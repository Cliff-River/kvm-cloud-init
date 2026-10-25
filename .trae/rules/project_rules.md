# kvm-cloud-init 项目规则

## 项目定位

多模板 / 多实例 KVM 虚拟机管理工具。Python 3.13，src 布局，uv 管理依赖，
console script 入口：`kvmci`（Python 包名仍为 `kvm-cloud-init`）。

## 架构边界（不要越层）

- `cli.py`：只做 argparse 参数解析、表格输出与异常到退出码的转换，不写业务逻辑。
  子命令：create/destroy/start/stop/reboot/status/list/templates/shell/exec/
  cloud-init/images。
- `config.py`：五份 YAML 配置（default/images/images.user/templates/instances）
  的加载、校验、三级合并（实例 > 模板 > 默认值），以及镜像目录三层合并
  （内置预设 > images.conf > images.user.conf）。
- `catalog.py`：内置主流发行版镜像预设（ID 为 `<家族>-<版本>[-变体]`），
  并确定性渲染系统 `images.conf`。
- `imagefetch.py`：镜像获取纯能力层——目录页解析、校验和解析、`.part`
  流式下载与原子替换、压缩包解压（tarfile/zipfile）、`images/.state.json`；
  HTTP 用 `httpx`，HTML 用 `parsel`。
- `imagefmt.py`：镜像格式纯能力层——按文件头探测 qcow2/raw，
  raw→qcow2 流式转换（纯标准库，不调用 `qemu-img`）。
- `cloudinit.py`：cloud-init 三份文档的渲染（实例内联 > 模板内联 > path 目录文件）；
  另含运行时把公钥注入合并后 `user-data`（追加到首个非 root 用户，无则新增 `cliff`）。
- `seediso.py`：临时目录 + pycdlib 生成卷标 `cidata` 的 ISO。
- `qcow2.py`：qcow2 头解析、容量单位、backing chain（纯标准库）。
- `ssh.py`：每实例密钥对（`keys/<实例名>`/`.pub`）的生成/复用/删除、
  paramiko 连接、交互 shell 与一次性 exec（不调用 `ssh`/`ssh-keygen` 子进程）；
  另有 `capture_command`（捕获输出）与 `stream_command`（流式跟随）供 cloud-init 使用。
- `hv/`：所有 libvirt 交互。`client`（连接/存储池/状态名）、`firmware`（UEFI 检测）、
  `storage`（卷上传/扩容/删除）、`domains`（共享关机 `shutdown_and_wait`/`stop_domain`、
  快照/undefine）、`network`（实测 IPv4：`virDomainInterfaceAddresses` lease→agent→arp）、
  `osinfo`（os-variant 到 libosinfo OS ID 的解析：本机 osinfo-db > 官方 GitLab 仓库 > 内置兜底）、
  `guest`（domain XML 生成与启动，含 `ssh-user` 元数据读写）。
- `provision/`：业务编排包，按职责拆分：`guard`（保护级别）、`create`（创建 + 等待）、
  `lifecycle`（start/stop/reboot/status/destroy）、`access`（shell/exec）、
  `listing`（list/templates）、`images`（images init/refresh/list/check/pull
  与 create 的 ensure_image）、`progress`（终端进度输出：rich 进度条与缩放自愈）、
  `cloudinit_wait`（等待/status/log）；公共 API 由 `__init__.py` 导出。
- `errors.py`：所有面向用户的异常，message 必须是可直接展示的英文。

## 硬性约定

1. 所有实例操作只连接 `qemu:///system`，禁止 `qemu:///session`。
2. 优先使用 Python 库（libvirt-python、pycdlib、PyYAML、httpx、parsel、rich、标准库），
   不新增 subprocess 调用；`hv/storage.py` 的 `sudo -n rm` 是已论证的最后兜底。
3. 不解析 CLI 输出：libvirt XML 用 `xml.etree`，容量信息用 qcow2 头，
   禁止用正则抠 XML/JSON。
4. cloud-init 文档覆盖以整份文件为粒度，不做 YAML 深合并。
5. 全部代码加类型注解；新增配置字段必须同步更新校验逻辑、配置示例与 README。
6. 存储资源按实例命名：`<实例名>.<镜像格式>`（通常 `.qcow2`，源镜像为 raw 时
   `.raw`）、`<实例名>-cidata.iso`。
7. 删除保护 `level`（normal/production/protected，缺省 normal）随三级合并；
   production 在 destroy/create 隐式销毁前需交互输入 yes（`--yes` 跳过，
   create 的 `--force` 等价跳过，内部仍优雅关机），非 TTY 必须安全失败；
   protected 对 `--yes`/`--force` 一律拒绝，只能改配置解除；把关逻辑在
   `provision/guard.py`，拒绝时抛 errors.ProtectionDenied，未登记实例按 normal。
8. SSH 用 paramiko（Ed25519 优先，无 `generate` 时回退 RSA-3072），
   禁止 subprocess 调用 `ssh`/`ssh-keygen`；密钥存于 git-ignored 的
   `keys/<实例名>`(`.pub`)，私钥 0600、公钥 0644，连接只用程序私钥、
   不做密码回退；登录用户记录在 domain 元数据 `ssh-user`。
9. destroy 在保护级别校验通过后同步删除 `keys/<实例名>` 与 `.pub`
   （缺失跳过，删除失败仅告警不阻断）；create 的隐式销毁不删密钥。
10. create 在 define + start 后默认等待 cloud-init（`--no-wait` 跳过）；
    等待经 SSH + `cloud-init status --format=json`（`json.loads`）轮询，
    IP/SSH 每 2 秒重试、最长 300 秒；失败打印摘要与日志尾部并退出 1，
    Ctrl+C 停止等待但实例保持运行。`--verbose` 固定 7 行滚动，非 TTY 降级为追加输出。
11. `stop` 与 `destroy` 共用 `hv/domains.shutdown_and_wait` / `stop_domain`，
    禁止第二套关机轮询；`stop --force` 不跳过优雅关机，仅超时后强制断电。
12. 所有面向用户的输出（argparse help、print、警告、异常、表头、状态名）用英文；
    代码注释与 docstring 可保持中文。
13. 镜像用 ID 引用：`templates.conf` 的 `image` 是镜像 ID，不得再出现
    `image_dir`（本地目录只由 `default.conf` 的 `image_dir` 决定）。
    系统 `images.conf` 由 `catalog.py` 确定性渲染（缺失自动生成、git-ignored、
    勿手改）；用户条目只写 `images.user.conf`（永不改写，同名键整条覆盖）。
14. 镜像获取统一走 `imagefetch.py`（HTTP 用 `httpx`、HTML 用 `parsel`；
    禁止 curl/wget/aria2 子进程）；下载写 `.<文件名>.part`，校验和通过后
    按需解压单文件压缩包（tarfile/zipfile），再由 `imagefmt.py` 探测实际格式、
    与条目 `format`（qcow2/raw，缺省按内容识别）不一致时转换（raw→qcow2），
    最后 `os.replace` 原子替换；失败不得破坏已有完整镜像；
    缺校验和且未标 `insecure: true` 时拒绝下载。禁止调用 `qemu-img` 子进程。
    解压/转换产物无法与上游哈希比对，`check` 以其 `.state.json` 的
    `upstream_file` 为准。
15. `default.conf` 的 `image_policy`（`auto`/`local`）决定 create 缺失镜像时
    是否联网；`create --local` 单次强制 local。本地镜像已存在时绝不联网、
    绝不替换；更新已有镜像只能显式 `kvmci images pull`。自动下载发生在
    保护级别检查与旧实例销毁之前。
16. `images check` 默认只查模板引用的镜像，`--all` 查整个目录；
    `images pull --all` 恰为模板引用镜像的去重集合；`pull` 无 ID 且无 `--all`
    时退出码 2。

## 常用命令

```bash
uv sync                  # 安装依赖
uv run pytest            # 单元测试（无需 root/KVM）
uvx ruff check src tests # 静态检查
uv run kvmci images init|refresh|list
uv run kvmci images check [ID ...] [--all] [--include-archived]
uv run kvmci images pull [ID ...] [--all] [--force]
uv run kvmci templates
uv run kvmci list
uv run kvmci create <实例名> [--template 模板] [--local] [--yes] [--force] [--no-wait] [--verbose]
uv run kvmci start|stop|reboot <实例名> [--force]
uv run kvmci status <实例名>
uv run kvmci cloud-init status <实例名>
uv run kvmci cloud-init log <实例名> [-f]
uv run kvmci shell <实例名>
uv run kvmci exec <实例名> -- <命令>
uv run kvmci destroy <实例名> [--force] [--yes]
```

## 测试要求

- 新增业务逻辑必须配 pytest 用例，测试放在 `tests/`。
- 禁止在单测中建立真实 libvirt 连接或启动虚机；libvirt 对象用 fake/mock，
  XML 用字符串 fixture。
- 镜像相关测试必须注入 fake opener 或 monkeypatch，禁止真实联网；
  目录页/校验清单样例放 `tests/fixtures/`。
- 涉及删除文件/存储卷、undefine、关机的改动，必须有对应测试覆盖。

## 注意事项

- 五个 `.conf` 文件名虽为 .conf，但内容是 YAML。
- libvirt-python 的部分绑定不接受 flags 参数（如 `pool.info()`、`vol.info()`、
  `domain.create()`），返回的 info 是 list 而非具名元组，改动时先核对绑定签名。
- 静默 libvirt 默认错误日志的注册在 `hv/client.py`，业务需自行 try/except。
