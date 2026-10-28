# AGENTS.md

面向 AI Agent 的项目导航。改动代码前先读本文件与
`.trae/rules/project_rules.md`。

## 项目是什么

`kvm-cloud-init` 把 cloud qcow2/raw 镜像（含压缩包发布物）+ cloud-init NoCloud
种子自动化交付为 KVM 虚拟机，支持多 OS 模板与多实例。Python 3.13 + uv + src 布局。

## 目录结构

```
├── pyproject.toml              # uv 项目；console script: kvmci
├── default.conf                # 全局默认值（YAML）
├── images.conf                 # 系统生成的镜像目录（git-ignored，勿手改）
├── images.user.conf            # 用户镜像目录（同名条目整条覆盖系统定义）
├── templates.conf              # OS 模板定义（YAML）
├── instances.conf              # 实例定义（YAML）
├── templates/<名称>/            # cloud-init 三件套：
│                               #   meta-data / network-config / user-data
├── images/                     # 源 qcow2/raw 镜像 + .state.json 下载状态
│                               #   + .staging/ 下载/解压/转换暂存（git-ignored）
├── keys/                       # 每实例 SSH 密钥对（git-ignored）：<名称> / <名称>.pub
├── src/kvm_cloud_init/
│   ├── __init__.py             # __version__ + main() 入口
│   ├── cli.py                  # argparse: create/destroy/start/stop/reboot/status/
│   │                           #   list/templates/shell/exec/cloud-init/images
│   ├── config.py               # 配置加载、校验、三级合并、镜像目录合并
│   ├── catalog.py              # 内置镜像预设 + 确定性渲染 images.conf
│   ├── imagefetch.py           # 目录页解析、校验和、流式下载、压缩包解压、.state.json
│   ├── imagefmt.py             # 镜像格式探测（qcow2/raw）与 raw→qcow2 流式转换
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
│   │   ├── images.py           # images init/refresh/list/check/pull + ensure_image
│   │   ├── progress.py         # 终端进度输出（rich 进度条 + 缩放自愈）
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
- 镜像三层来源合并为一张注册表：内置 `catalog.PRESET` → 系统 `images.conf`
  （缺失时自动确定性渲染、git-ignored、勿手改）→ 用户 `images.user.conf`
  （永不改写，同名键整条覆盖）。`templates.conf` 的 `image` 是镜像 ID，
  不再有 `image_dir`（本地目录只由 `default.conf` 的 `image_dir` 决定）。
- 镜像获取：`image_policy`（`auto`/`local`）来自 default.conf，`create --local`
  单次强制 local。本地文件已存在时绝不联网、绝不替换；缺失时 auto 先
  `resolve_target`（url 直链 / index 目录页爬虫）→ 校验和比对 → 在
  `images/.staging` 的临时目录内流式下载 → 校验通过后按需解压压缩包
  （`.tar.xz` 等，tarfile/zipfile 标准库）→ `imagefmt.detect` 探测实际格式
  → 与条目声明的 `format` 不一致时转换（raw→qcow2）→ 原子替换，
  然后才进入保护级别检查与旧实例销毁。
  更新已有镜像只能显式 `kvmci images pull`。
- `images check` 默认只查模板引用镜像，`--all` 查整个目录；`images pull --all`
  恰为模板引用镜像的去重集合；`pull` 无 ID 且无 `--all` 退出 2。
- 删除保护字段 `level`（normal/production/protected，缺省 normal）同样三级合并；
  destroy 与 create 的隐式销毁在 `provision/guard.py` 统一把关：production 需交互输入
  yes（`--yes` 跳过；create 还可用 `--force`，但内部仍走优雅关机），非 TTY
  安全失败；protected 对任何标志都拒绝，只能改配置解除。未登记实例按 normal。
- 创建：生成/复用 `keys/<名称>` 密钥对 → 渲染文档并把公钥运行时注入合并后
  `user-data`（追加到首个非 root 用户，无则新增 `cliff`）→ 临时目录生成
  cidata.iso（pycdlib）→ stream 上传镜像与 ISO 到存储池（按实例命名卷，
  卷名后缀与 disk driver 跟随源镜像探测格式 qcow2/raw）→
  可选卷扩容 → 构建 domain XML（含 `ssh-user` 元数据）→ define + start →
  默认等待 cloud-init（`--no-wait` 跳过，`--verbose` 7 行滚动）。
- 生命周期：`start` 用 `dom.create()`（本绑定不接受 flags），`stop` 与 `destroy` 共用
  `hv/domains.shutdown_and_wait` / `stop_domain`（禁止第二套轮询）；
  `reboot` 用 `dom.reboot(0)`；`status` 只读打印六项键值。
- cloud-init：用 paramiko 执行 `cloud-init status --format=json`（`json.loads`），
  `log -f` 走 `ssh.stream_command` 跟随 `/var/log/cloud-init.log`。
- 销毁：优雅关机轮询 → 收集域/快照 XML 中外部文件并追 backing chain →
  删快照 → undefine（NVRAM/checkpoints/TPM 标志位）→ 清理残留卷与文件 →
  删除 `keys/<名称>` 与 `<名称>.pub`（保护级别未通过则不删）。
- shell/exec：从域 XML 读 `ssh-user` → libvirt 实测 IPv4
  （lease→agent→arp，见 hv/network.py）→ 用 `keys/<名称>` 私钥经 paramiko
  建立交互 shell 或执行命令；exit code 透传。`shell` 与 `exec` 在实例关机时
  都先复用 `lifecycle.start_instance` 自动启动，再等待 IP 就绪。本地
  stdin/stdout 均为 TTY 时 `exec` 申请 PTY 并双向转发（`exec <实例> -- bash`
  可用），重定向/管道场景不分配 PTY。
- 连接统一为 `qemu:///system`；用户需在 libvirt/kvm 组。
- 所有面向用户的输出（help、print、警告、错误、表头、状态名）均为英文。

## 开发与验证

```bash
uv sync
uv run pytest
uvx ruff check src tests
uv run kvmci images init
uv run kvmci images refresh
uv run kvmci images list
uv run kvmci images check --all
uv run kvmci images pull --all
uv run kvmci templates
uv run kvmci list
uv run kvmci create <实例名> [--local] [--no-wait] [--verbose]
uv run kvmci status <实例名>
uv run kvmci start|stop|reboot <实例名>
uv run kvmci cloud-init status <实例名>
uv run kvmci cloud-init log <实例名> [-f]
uv run kvmci shell <实例名>
uv run kvmci exec <实例名> -- <命令>
```

改动后至少保证 `uv run pytest` 全绿；改了配置字段或 CLI 行为要同步
README（中英双语）、五个 `.conf` 示例（含 `images.user.conf`）与本文件。

## 禁止事项

- 不要重新引入 Bash 脚本或 subprocess 包装 virsh/virt-install/genisoimage/qemu-img，
  也不要用 subprocess 调用 `ssh`/`ssh-keygen`（统一走 paramiko）。
- 镜像下载统一走 `imagefetch.py`（HTTP 用 httpx，HTML 用 parsel），
  不要调用 curl/wget/aria2 子进程；压缩包解压用标准库 `tarfile`/`zipfile`，
  格式转换用 `imagefmt.py`，都不要调 `qemu-img` 子进程。
- 不要手改 `images.conf`（系统生成、git-ignored）；用户条目写入 `images.user.conf`。
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
- SSH 连接重试在 `ssh._connect` 内：连接/握手失败每 `CONNECT_RETRY_INTERVAL`
  （2s）重试到 `deadline`（`time.monotonic()` 绝对时间，缺省
  `now + CONNECT_RETRY_TIMEOUT`=60s），认证失败立即失败不重试；
  `shell`/`exec` 由 `access` 传入与 IP 等待共用的 300 秒 deadline，
  首次重试打印 `Waiting for SSH on <ip> ...`。实例刚启动时 IP 常早于 sshd
  就绪，只等 IP 会在握手阶段直接报 `Unable to connect to port 22`。
- `virDomainInterfaceAddresses` 支持 SRC_LEASE / SRC_AGENT / SRC_ARP 三类来源。
- `imagefetch.py` 的 HTTP 客户端可注入：测试用
  `httpx.Client(transport=httpx.MockTransport(handler))`，不建立真实连接。
- `imagefmt.py` 纯标准库实现格式探测与 raw→qcow2 流式转换：qcow2 头 104 字节、
  64 KiB 簇、refcount order 4；数据簇顺序追加，L1/L2 项都写 `COPIED` 标志，
  全零簇不分配（`qemu-img check` 0 错误）。虚拟大小按 512 字节扇区向上对齐
  （qemu 向下取整）；不要对输出 `truncate()` 到当前位置（会截断成 104 字节）。
- 压缩包解压用标准库 `tarfile`/`zipfile`（`.tar.xz` 走 `r:xz`），要求包内恰好
  一个普通文件；`extract_archive` 的压缩格式由 `filename` 参数判定——暂存
  文件名为 `source`（无后缀），无法据此识别。解压时全零块 `seek` 前进保持稀疏。
- 解压/转换后的镜像哈希与上游发布物不可比：`.state.json` 记录
  `format`/`source_format`/`transformed`，`check` 对 transformed 条目只比对
  `upstream_file`。`kali-linux` 预设声明 `format: qcow2`，其 `.tar.xz` 内是
  25 GiB 的 `disk.raw`。
- 下载进度由 `provision/progress.DownloadProgress` 提供（表现层，与编排解耦）：
  TTY 用 rich 渲染（彩条、已下载/总量、速度、ETA），并把 `Progress` 内部的
  live 替换为 `_ResizeAwareLive` 子类——终端**缩窄**时按旧帧在新宽度下
  reflow 的视觉行数逐行相对清除（上移 rows−1 次，多一次会抹掉上方下载
  提示行），变宽不介入；rich 自动刷新线程 10Hz 出帧，缩放后最迟下一帧
  自愈，无需自行处理 SIGWINCH。非 TTY 每 5 秒追加 `current / total` 行；
  下载结束由 `imagefetch.download` 统一 `close()`，`close()` 不会把进度补满。
  `provision/images.py` 只依赖 `imagefetch.Progress` 协议、不 import rich；
  `pull_images` 的 `progress_factory` 默认 `None`，由 `cli` / `create` 显式
  传入 `progress_printer`（`provision/__init__.py` 导出）。
  AlmaLinux 等源用 chunked GET（无 Content-Length），`_remote_size` 会用 HEAD
  补总大小；HEAD 不可用时 `_DownloadSizeColumn` 只显示已下载字节，
  不出现 `? MB`。Ctrl+C 由 `cli.main` 捕获，`staging_area` 退出时清理当前
  暂存目录，随后输出 `Operation cancelled.` 并退出 130。
- `ssh.run_command` 的 TTY 分支必须保留 `exec_command()` 返回的 stdin/stderr
  文件对象：只取 stdout 会让 stdin 文件被 GC 并触发 `shutdown_write()`，
  通道立即 EOF，交互式命令卡死。
- 镜像下载、解压与转换都在 `<image_dir>/.staging/<临时目录>` 内完成，校验通过后
  用 `os.replace` 原子落盘。暂存目录必须与最终镜像同一文件系统（同盘保证 rename
  不跨设备），因此只能用 `tempfile.TemporaryDirectory(dir=...)` 指定到 `.staging`，
  不能用默认 `/tmp`。下载进程在目录内 `.lock` 上持有排他 `flock`；进程被强杀后
  的残留目录由 `imagefetch.sweep_staging` 在下次下载前回收——能立刻获得锁且
  超过 `STAGING_MAX_AGE`（1 小时）才删除，活跃下载不会被误删。
  失败只清理暂存目录，绝不破坏已有完整镜像。`images/.state.json` 仅记录最近
  一次成功下载。
- `catalog.py` 渲染必须确定性（同一预设始终产出同一份 `images.conf`）；
  `images.conf` 的上游探测结果只写入 `upstream_*` / `checked_at` 元数据字段。
- 校验清单兼容 coreutils（`<hash>  <file>`）与 BSD（`SHA256 (<file>) = <hash>`），
  以及 Fedora 的 PGP 包裹清单；缺校验和且未标 `insecure: true` 时拒绝下载。
