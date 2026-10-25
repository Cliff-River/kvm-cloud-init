# 需求：主流发行版云镜像目录、自动下载与版本爬虫

> 本文是给实现者的需求说明。按“目标与范围 → 配置设计 → 下载与爬虫能力 →
> CLI 规格 → create 集成 → 模块与错误 → 测试 → 文档 → 验收”的顺序阅读，
> 逐项落实。与 `.trae/rules/project_rules.md` 的硬性约定冲突时，以本文为准
> （本文显式要求的改动除外，其余约定继续生效）。

## 一、目标与范围

把“源 qcow2 镜像必须手工预先放进 `images/`”升级为**声明式镜像目录管理**：

1. 工具内置一份覆盖**市场主流发行版及其多个版本**的镜像预设（随代码发布），
   一次性生成系统镜像定义文件，开箱即包含如 AlmaLinux 8/9/10、
   Rocky 8/9/10、CentOS Stream 9/10、Debian 11/12/13、
   Ubuntu 20.04/22.04/24.04/26.04、Fedora 等条目；
   一条命令即可刷新目录定义并探测全部条目的上游更新。
2. **系统自动生成的镜像定义与用户自定义定义分离为两个配置文件**：
   - `images.conf`：系统生成（工具从内置预设渲染、可刷新），文件头标注
     DO NOT EDIT，缺失时自动生成；
   - `images.user.conf`：用户自定义镜像源，工具只读取、永不改写。
3. `templates.conf` **不再定义 `image_dir`**；`image` 字段由“本地文件名”
   改为“引用镜像目录中的镜像 ID”，系统条目与用户条目均可被引用。
4. `create` 在本地镜像缺失时可**自动下载**（默认行为）；同时提供
   **强制仅使用本地镜像**的策略（全局配置 + 单次命令开关），离线环境
   不触网。
5. 新增 `kvmci images` 子命令：初始化/刷新目录、列出、探测更新、拉取
   （`pull --all` 一次性下载**模板实际引用到的**全部镜像；按需也可按
   镜像 ID 拉取目录中的任意发行版，如 `almalinux-9`、`debian-12`）。
6. 提供通用目录页爬虫（`kind: index`）：解析 HTTP 目录列表、按文件名
   版本/日期挑选最新、读取同目录校验清单；新增发行版只需在用户文件中
   加配置，不写代码。
7. 下载全程校验和验证，临时文件 + 原子重命名，失败不污染已有镜像。

**非目标（不要做）**：

- 不提供“一键下载目录内全部发行版镜像”的能力（数十 GiB 级别，无实际
  场景）；批量拉取的边界是**模板引用集合**，目录全集只做元数据探测。
- 不做后台定时更新 / systemd timer / cron；刷新与拉取只能由显式命令触发。
- 不在 `create` 时静默替换**已存在**的本地镜像（缺失才自动下载；
  版本更新只能显式 `kvmci images pull`）。
- 不做镜像格式转换、压缩解压、去重、P2P/多线程下载加速。
- 不新增第三方 HTTP 依赖（requests/httpx 等），不引入
  curl/wget/aria2c 子进程；统一标准库 `urllib`。
- 不改 cloud-init 文档渲染、密钥策略、libvirt 上传与 domain XML 逻辑；
  `create` 除“确保镜像存在”这一步外，后续流程与输出语义不变。
- 不做图形界面。

## 二、配置设计

镜像定义分三层来源，加载后合并为一个镜像注册表：

| 层 | 载体 | 归属 | 工具是否改写 |
| --- | --- | --- | --- |
| 内置预设 | 包内数据/代码常量（随版本发布） | 系统 | 随工具版本更新 |
| 系统定义 | 项目根 `images.conf` | 系统 | **自动生成/刷新，用户勿手改** |
| 用户定义 | 项目根 `images.user.conf` | 用户 | **永不改写，缺失即可** |

### 2.1 内置预设与 `images.conf`（系统生成）

内置预设是覆盖主流发行版的**目录模板**（每个家族一条 index/url 规则，
版本号作为参数），由工具渲染为完整的 `images.conf`。

**首批必须覆盖的家族与版本**（x86_64；实现时逐个核对 2026-10 时点的
真实 URL、清单文件名与命名规律，核对结果固化进预设）：

| 家族 | 版本（至少包含） | 镜像变体 | 典型源 kind |
| --- | --- | --- | --- |
| AlmaLinux | 8 / 9 / 10 | GenericCloud qcow2 | `url`（latest 符号链接） |
| Rocky Linux | 8 / 9 / 10 | GenericCloud 与 LVM 两个变体 | `index` |
| CentOS Stream | 9 / 10 | GenericCloud qcow2 | `index` |
| Debian | 11 / 12 / 13 | generic cloud amd64 qcow2 | `index` |
| Ubuntu | 20.04 / 22.04 / 24.04 / 26.04 | server cloudimg amd64 | `url`/`index` |
| Fedora | 当前正式版与前一版本 | Cloud qcow2 | `index`（版本随 refresh 滚动） |

- EOL 版本（如未来的 Rocky/CentOS 8、Debian 11、Ubuntu 20.04）预设中
  标注 `archived: true`，使用各家族归档站（vault / archive /
  old-releases）规则；`check` 默认跳过 archived 条目（`--include-archived`
  可包含），`pull <ID>` 对 archived 条目仍可工作。
- openSUSE Leap/Tumbleweed JeOS、Oracle Linux 作为**尽力而为**条目：
  规则能在交付前核对通过就纳入，否则不进首批，不因此阻塞整体交付。
- 每个条目的 ID 命名稳定可预测：`<家族>-<版本>[-变体]`，如
  `almalinux-9`、`debian-12`、`rocky-10-lvm`、`ubuntu-24.04`、
  `centos-stream-9`。现有四个模板引用的 `rocky-10-lvm` /
  `ubuntu-26.04` / `debian-13` / `almalinux-10` 必须存在且命名一致。
- 预设是纯数据（无网络行为）；网络探测只发生在 `check` / `refresh` /
  `pull` 中。

**`images.conf` 生成规则**：

- 由内置预设**确定性渲染**生成；文件头写明：
  “由 kvmci 自动生成，请勿手工编辑；自定义镜像请写入 images.user.conf；
  执行 `kvmci images refresh` 重建本文件”。
- 缺失时 `ConfigStore.load()` 自动渲染生成（纯本地操作，不触网），
  因此可安全加入 `.gitignore`；也可用 `kvmci images init` 显式生成。
- `refresh` 重建该文件，并在探测成功时为每个条目写入**注释性元数据字段**
  （见 2.4 状态来源）：`upstream_file` / `upstream_version` / `checked_at`；
  探测失败的条目保留上次值并标注 `unreachable`，不使 refresh 失败。
- 手工修改该文件：下次 `refresh` 直接整体覆盖，不做合并、不做迁移提示。

### 2.2 `images.user.conf`（用户自定义）

- 与系统条目同 schema 的 YAML 根映射；文件不存在视为空表，不报错；
  文件存在但格式非法仍报 `ConfigError`。
- 用户条目可以**新增**任意 ID（内部源、自定义封装镜像、本地 file:// 等），
  也可以**同名覆盖**系统 ID（替换其全部定义，不做字段级深合并）；
  覆盖时合并结果以用户为准，`images list` 的 `Origin` 列显示 `user`
  （新增）或 `user(override)`（覆盖），便于发现。
- 工具任何命令都不得写该文件；它是用户 git 跟踪、代码评审的对象。
- 两个文件都不得再包一层 `images:` 包装键（`images` 为保留名，
  检测到旧包装键时报错提示去缩进，逻辑复用 `_load_table`）。

### 2.3 条目 schema（系统与用户通用）

顶层每个键即镜像 ID。通用字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 下载后在 `image_dir` 中保存的**固定文件名**（与上游真实文件名解耦） |
| `kind` | 是 | `url`（固定直链）或 `index`（目录页爬虫） |
| `arch` | 否 | 仅展示用，如 `x86_64`；不参与解析 |
| `checksum_algo` | 否 | `sha256`（默认）/ `sha512` |
| `insecure` | 否 | `true` 时允许无校验和下载；默认 `false`，缺校验和即失败 |
| `archived` | 否 | 系统预设使用；用户条目标注也生效（check 默认跳过） |

`checksum_file`（`index` 种）与 `checksum_url`（`url` 种）二选一；
`index` 种在多版本共用清单（如 AlmaLinux 的 `CHECKSUM`）时可改用
`checksum_match` 正则从目录页挑选清单文件。

**`kind: url`（固定直链，适合文件名固定、原地更新的源）**：

```yaml
almalinux-10:
  file: AlmaLinux-10-GenericCloud.x86_64.qcow2
  kind: url
  urls:
    - https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2
    # 国内镜像回退（按需取消注释）：
    # - https://mirrors.ustc.edu.cn/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2
  checksum_url: https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2.CHECKSUM
```

- `urls`：1~N 个**有序镜像地址**，按顺序尝试；连接失败 / 超时 / 404 /
  5xx 自动回退下一个，全部失败才报错。

**`kind: index`（目录页爬虫，适合文件名带日期/版本号的源）**：

```yaml
rocky-10-lvm:
  file: Rocky-10-GenericCloud-LVM.x86_64.qcow2
  kind: index
  pages:
    - https://download.rockylinux.org/pub/rocky/10/images/x86_64/
  match: 'Rocky-10-GenericCloud-LVM-[\d.]+-\d{8}\.\d+\.x86_64\.qcow2$'
  order_by: name            # name（默认，文件名字典序最大）| mtime（HEAD Last-Modified）
  checksum_file: CHECKSUM   # 目录页内校验清单文件名
  checksum_algo: sha256
```

- `pages`：1~N 个**有序目录页地址**（官方/镜像回退），取第一个可访问页。
- `match`：匹配 href basename 的正则；加载期必须可编译，非法正则
  报 `ConfigError`。
- `order_by: name`（默认）按 basename 字典序取最大（`YYYYMMDD` 与
  版本号字典序即时间序）；`mtime` 逐个 HEAD 取 `Last-Modified` 最新，
  HEAD 不支持时回退 name 并打印一次英文警告。
- `checksum_file`：清单 URL 由目录页 URL 与文件名 join 得到；清单内
  可能含多个文件哈希，按**上游真实文件名精确匹配对应行**。
- `checksum_match`：当清单文件名不固定（如带版本号）时，用正则从目录页
  href 中挑选清单文件；与 `checksum_file` 互斥，二者都不给则该源无校验和
  （需 `insecure: true` 才能下载）。
- 爬虫解析出的真实文件名/版本号只写入生成元数据与状态文件，
  不回写 `images.user.conf`，也不需要用户手改进系统文件。

### 2.4 `templates.conf` 改动

- **删除 `image_dir` 字段**（含全部现有条目与文件头注释中的字段说明）。
  镜像目录只由 `default.conf` 的全局 `image_dir` 决定；改动后
  `image_dir` 进入模板未知字段集合，出现即 `ConfigError`。
- `image` 字段语义改为 **镜像 ID**（系统或用户条目均可），例如：

```yaml
Rocky-LVM:
  path: templates/Rocky-LVM
  image: rocky-10-lvm
  os_variant: rocky10

AlmaLinux-minikube:
  path: templates/AlmaLinux-minikube
  image: almalinux-10        # 与 AlmaLinux 模板共用同一 ID
  os_variant: almalinux10
  memory: 4096
  vcpus: 6
  capacity: 40G
```

- `ConfigStore.load()` 增加交叉校验：每个模板的 `image` 必须能在合并后
  的镜像注册表中找到，否则报 `ConfigError`（参照实例→模板的现有写法）。

### 2.5 `default.conf` 新增字段

```yaml
# 镜像缺失时的获取策略：
#   auto  本地镜像缺失时自动从镜像目录定义的源下载（默认）
#   local 仅使用本地镜像，缺失即失败，绝不触网（离线/受限网络环境）
image_policy: auto
```

- 枚举校验：仅允许 `auto` / `local`；缺省 `auto`（该行可省略）。
- 现有 `image_dir: images` 保持不变，成为镜像目录的唯一来源。
- 不新增实例级 / 模板级 policy 覆盖；策略只在全局与命令行开关两个层次。

### 2.6 本地状态文件 `images/.state.json`

- 位于已被 git 忽略的 `images/` 目录，记录每个 ID 最近一次成功下载的
  事实；手工删除可接受（丢失版本记忆，下次 check/pull 重建）：

```json
{
  "rocky-10-lvm": {
    "file": "Rocky-10-GenericCloud-LVM.x86_64.qcow2",
    "url": "https://download.rockylinux.org/.../Rocky-10-GenericCloud-LVM-10.2-20260525.0.x86_64.qcow2",
    "version": "10.2-20260525.0",
    "checksum_algo": "sha256",
    "checksum": "<hex>",
    "size": 201334272,
    "fetched_at": "2026-10-25T12:00:00Z"
  }
}
```

- 文件损坏 / JSON 非法：忽略整个文件并按空状态继续（打印一次英文
  警告），不得导致命令失败。
- 状态只用于版本展示，不作为可信性依据；可信性只由本次下载校验和决定。
- `images.conf` 中由 refresh 探测写入的 `upstream_*` 字段是**目录元数据
  缓存**（让 `list` 离线也能展示上游版本）；`images/.state.json` 是
  **本机下载事实**。二者职责不同，不得合并。

## 三、下载与爬虫能力

### 3.1 网络与重试

- 仅使用标准库：`urllib.request` / `urllib.parse` / `html.parser` /
  `hashlib` / `json`。不新增依赖，不调子进程。
- 自定义 `User-Agent: kvmci/<version>`；连接与读取超时均 30 秒。
- 重试：单个 URL 最多 3 次尝试，指数退避（2s、4s）；触发条件为
  网络错误 / 超时 / 5xx；404 立即切换下一个镜像地址。所有候选均失败
  时抛 `ImageFetchError`，信息包含尝试过的地址与最后一次错误。
- 下载前响应带 `Content-Length` 时用 `os.statvfs` 比对目标目录剩余
  空间，不足直接失败（不产生半截文件）。

### 3.2 目录页解析（kind: index）

- 用 `html.parser.HTMLParser` 提取全部 `href`，`urllib.parse.urljoin`
  还原绝对地址后对 basename 应用 `match`；禁止用正则抠 HTML 标签。
- 目录页按 `pages` 顺序回退；某页可访问但零匹配时，报错信息包含页面
  URL 与正则文本，便于排查上游改版。
- 解析与挑选做成纯函数，目录页 HTML 以 fixture 形式进测试。

### 3.3 校验清单

- 必须支持 coreutils 两列格式 `<hex-hash>  <filename>`（两个空格，
  文件名可能以 `*` 开头表示二进制模式），按上游真实文件名取对应行；
  哈希长度须与声明算法一致。
- 忽略注释行（`#` 开头）与空行；一个清单匹配到多行或零行均视为失败。
- 无清单 URL（`url` 种缺 `checksum_url`、`index` 种缺 `checksum_file`）
  时：仅当 `insecure: true` 才允许跳过并打印警告，否则失败。

### 3.4 落盘协议（下载与校验解耦）

1. 流式下载（1 MiB 块）到 `image_dir` 内临时文件 `.<file>.part`，
   下载同时增量计算哈希；同名残留 `.part` 启动时先删除。
2. 完成后比对清单哈希；不匹配则删除临时文件并重试（计入重试次数），
   最终失败不得触碰已有正式文件。
3. 校验通过后 `os.replace()` 原子重命名为 `file`，再更新
  `.state.json`（先写临时 JSON 再原子替换）。
4. 任何异常路径都不得留下半个正式镜像；正式镜像一旦存在即保持上一个
   完整版本。

### 3.5 输出

- 下载进度：TTY 下单行原地刷新（已下载 MiB / 总 MiB / 百分比 / 速率）；
  非 TTY 每 15 秒逐行追加一次，不输出 ANSI 控制符。
- 全部面向用户文本为英文；注释/docstring 可中文。

## 四、CLI 规格

### 4.1 新增 `images` 子命令

```
kvmci images init
kvmci images refresh [--include-archived]
kvmci images list
kvmci images check [ID ...] [--all] [--include-archived]
kvmci images pull  [ID ...] [--all] [--force]
```

ID 位置参数既接受**镜像 ID**（含目录中任意条目，如 `almalinux-9`、
`debian-12`），也接受**模板名**（解析到该模板的 `image` ID，去重）。

- `init`：从内置预设渲染生成 `images.conf`；已存在时拒绝覆盖并提示
  用 `refresh`（`--force` 可强制重建，用户文件不受影响）。不触网。
- `refresh`：从内置预设重建 `images.conf`（用户文件不动），并逐个探测
  条目上游、把最新文件名/版本/探测时间写入生成文件的元数据字段；
  默认跳过 `archived` 条目。单项探测失败记录为 `unreachable` 并继续，
  全部失败也退出 0（仅网络全不可用时退出 1）。不下载镜像。
- `list`：只读、不触网。表格列：
  `Image ID` / `Origin`（system / user / user(override)）/
  `Local`（present/missing）/ `Kind` / `Catalog ver.`（生成文件中的
  上游版本缓存，无则 `-`）/ `Local ver.`（state 中版本，无则 `-`）。
- `check`：只探测不下载。默认跳过 archived；`--all` 探测目录全部
  非归档条目。逐项输出 `up to date` / `update available` / `missing` /
  `unreachable`；单项失败不中断批量；存在任一可更新或失败时退出码 1，
  全部最新退出 0。
- `pull`：
  - 不带参数且不带 `--all`：参数错误，退出码 2；
  - `--all`：处理 `templates.conf` 引用到的全部镜像 ID（去重）——
    这是“**一次性把当前要用的主流镜像及更新拉齐**”的入口；
  - 本地缺失 → 下载；本地存在且上游相同 → 跳过提示；上游更新 →
    下载新版并原子替换；`--force` 忽略版本判断强制重下；
  - 单项失败记录后继续，末尾汇总成功/失败数，有失败退出 1；
  - `pull` 是唯一允许替换已存在本地镜像的命令；
  - 目录中未被任何模板引用的条目（如 `almalinux-9`、`debian-12`）
    用显式 ID 拉取，`--all` 不包含它们。
- `images` 子命令均不需要 libvirt 连接（与 `templates` 同级，在
  `connect()` 之前分派）。

### 4.2 `create` 改动

新增开关：

```
kvmci create <实例名> [--template ...] [--local] [--yes] [--force] [--no-wait] [--verbose]
```

- `--local`：本次创建强制仅使用本地镜像（等价于临时把 `image_policy`
  视为 `local`），缺失即失败、绝不触网。
- 上传镜像之前，把现有的“文件存在性检查”替换为
  `provision.ensure_image(...)`，行为见第五节。
- 其余参数与流程不变。

## 五、`create` 集成：缺失自动下载 / 强制本地

`ensure_image` 决策表：

| 本地文件 | 策略 | 行为 |
| --- | --- | --- |
| 存在 | auto / local | 直接使用，不触网、不校验、不替换 |
| 缺失 | auto（默认） | 解析源（url 直连或 index 先爬虫定位最新）→ 下载 → 校验 → 原子落盘 → 继续创建；失败抛 `ImageFetchError`，退出 1 |
| 缺失 | local（配置）或 `--local` | 抛 `ImageNotFound`，英文信息提示可用 `kvmci images pull <ID>` 预下载或改用 auto 策略；退出 1，不触网 |

- 自动下载发生在保护级别检查、销毁旧实例**之前**：下载失败时机器
  现状不发生任何改变。
- 自动下载复用与 `images pull` 完全相同的下载/校验/落盘实现，
  禁止第二套下载逻辑。
- 已存在本地镜像永远原样使用——即便是 `index` 条目已有新版本，
  `create` 也不更新；需要更新走显式 `pull`。

## 六、模块分层、配置对象与错误

```
src/kvm_cloud_init/
├── catalog.py           # 内置主流发行版预设（纯数据）+ 渲染 images.conf
│                        #   的纯函数 + ID/版本命名规则；不触网
├── imagefetch.py        # 纯能力层（不依赖 config/provision/hv）：
│                        #   resolve_url/parse_index/pick_latest/
│                        #   fetch_checksum/download_verify/state 读写
├── config.py            # 加载校验 images.conf（缺失时调 catalog 渲染）
│                        #   与 images.user.conf，合并注册表（用户覆盖系统）；
│                        #   ImageSource 数据类；Template 删除 image_dir、
│                        #   image 改为 ID 语义；Defaults 增加 image_policy；
│                        #   resolve() 经注册表计算 image_path
├── errors.py            # 新增 ImageFetchError(KciError)
└── provision/
    └── images.py        # 编排：init/refresh/list/check/pull/ensure_image
                         #   （由 __init__ 导出）
```

- `config.py`：
  - 新增 `@dataclass ImageSource`：id、file、kind、urls/pages、match、
    order_by、checksum_*、insecure、archived、arch、upstream_*（可空）；
    全部加类型注解。
  - 系统文件缺失 → 调 `catalog.render_catalog()` 生成；用户文件缺失
    → 视为空表。合并后记录每个 ID 的来源（system / user /
    user(override)）供 list 使用。
  - `Template` 删除 `image_dir`；`ResolvedInstance` 增加 `image_id: str`，
    `image_path` 改为 `root / defaults.image_dir / <ImageSource.file>`。
  - 加载期 `ConfigError` 覆盖：未知字段、坏 kind、match 非法正则、
    空 urls/pages、未知 checksum_algo、缺必填、跨两文件合并后 `file`
    重复（两个 ID 落同一本地文件会让状态与磁盘语义冲突）、模板 `image`
    引用不存在的 ID、模板出现 `image_dir`、`image_policy` 非法枚举。
- `errors.py`：新增 `ImageFetchError(KciError)`（下载、爬虫、校验、
  镜像全不可达等）；本地缺失继续用现有 `ImageNotFound`。
- `cli.py`：只加 argparse 定义、表格/退出码转换；业务全部走
  `provision`。
- 不引入任何 subprocess、不新增第三方依赖（`pyproject.toml`
  dependencies 不变）。

## 七、测试要求（pytest，全部 mock，不触网、不连 libvirt、不启动虚机）

1. **catalog 预设与渲染**：
   - 渲染结果是确定性字符串；包含第一节要求的全部家族/版本 ID；
   - 每个条目 schema 自洽（必填齐全、kind 与字段匹配）；
   - 预设中全部非 archived 条目的 `match` 可编译；ID 无重复、`file`
     无重复。
2. **config 校验与合并**：
   - 系统文件缺失时自动生成且不触网；用户文件缺失视为空；
   - 用户新增 ID 生效；同名覆盖系统条目且来源标记为 user(override)；
   - 合并后 file 重复、用户文件 YAML 非法、坏 kind、match 非法、
     空候选、未知 algo、缺必填均报 `ConfigError`；
   - 模板 `image` 引用不存在 ID → 报错；模板出现 `image_dir` → 报错；
   - `image_policy` 非法枚举 → 报错；默认 auto。
3. **index 解析纯函数**：把 Rocky/Debian/Ubuntu/CentOS 风格目录页
   HTML 与 CHECKSUM/SHA*SUMS 存为 `tests/fixtures/`，断言 href 提取、
   urljoin、正则匹配、name 选最新、mtime（伪造 HEAD）选最新及回退、
   清单多文件行精确匹配。
4. **下载器（注入 fake opener）**：
   - 流式下载成功且哈希正确 → 原子落盘 + state 更新；
   - 哈希不匹配 → 临时文件删除、正式文件（预先放置）原样保留、
     触发重试；
   - 第一个 URL 404/超时 → 回退第二个；全失败 → `ImageFetchError`；
   - 中途异常 → 无 `.part` 残留；state 损坏 → 警告并以空状态继续；
   - Content-Length 大于剩余空间（伪造 statvfs）→ 直接失败；
   - 无校验清单且非 insecure → 失败；insecure → 成功且有警告。
5. **provision/images 编排**：
   - `ensure_image` 决策表四格（存在×两策略、缺失×两策略）；
   - `init` 已存在拒绝覆盖、`--force` 重建且不动用户文件；
   - `refresh` 成功写入 upstream 元数据、单项 unreachable 不失败、
     archived 默认跳过；
   - `pull --all` 的目标恰好是模板引用 ID 集合（含共用 ID 去重），
     跳过最新、更新有新版、`--force` 重下、单项失败继续且退出码正确；
   - 模板名位置参数解析到镜像 ID。
6. **create 集成**：缺失 + auto 时下载发生在旧实例销毁之前
   （调用顺序断言）；`--local` 缺失直接 `ImageNotFound`；下载成功后
   上传路径为注册表 `file` 解析的路径。
7. CLI：`images` 不调用 connect；`pull` 无参数无 `--all` 退出码 2；
   表头与输出全英文。
8. 涉及文件删除/覆盖（.part、原子替换、init/refresh 重建）的路径
   必须有用例覆盖。

完成后 `uv run pytest` 全绿，Ruff 无告警。

## 八、文档同步

1. `README.md`（中英双语）：
   - 新增“镜像目录 / Image catalog”章节：三层来源、系统文件与用户
     文件分工、主流发行版覆盖范围、`init/refresh/check/pull` 工作流、
     镜像回退与国内镜像注释候选、自动下载策略与 `--local`；
   - CLI 表格新增 `images` 子命令族；`create` 补充 `--local` 与
     “缺失自动下载”说明；
   - 快速开始体现首次 create 自动下载，或先 `kvmci images pull --all`；
   - 给出自定义内部镜像源的最小 `images.user.conf` 示例。
2. `AGENTS.md`：目录树加 `catalog.py`、`imagefetch.py`、`images.conf`
   （generated）与 `images.user.conf`；更新 config/provision 职责与
   常用命令；说明 `image` 字段新语义、`image_dir` 收口到 default。
3. `.trae/rules/project_rules.md`：配置清单更新为“default/templates/
   instances + 系统生成 images.conf + 用户 images.user.conf”；
   images 子命令、image_policy、自动下载规则、模板字段变化入文。
4. `.gitignore`：新增 `/images.conf`（生成产物）；`images.user.conf`
   保持被跟踪；`images/` 现有规则不变。
5. `templates.conf` 文件头注释删除 image_dir、更新 image 语义；
   新增 `images.user.conf` 示例文件（含注释化字段说明与一个例子，
   示例不定义真实条目，避免与系统 ID 冲突）。

## 九、验收标准

```bash
uv run pytest                                  # 全绿；ruff 无告警

uv run kvmci images list                       # 展示目录全家桶；不触网
                                               # 含 almalinux-9 / debian-12 等
test -f images.conf || echo "auto-generated on first load"
uv run kvmci images init                       # 显式生成 images.conf
uv run kvmci images refresh                    # 重建目录并探测上游版本，不下载镜像
uv run kvmci images check --all                # 探测全部非归档条目更新情况
uv run kvmci images pull --all                 # 一次性拉齐模板引用的镜像
uv run kvmci images pull almalinux-9           # 按需拉取目录中的任意发行版
uv run kvmci images pull debian-12
rm images/<某镜像文件>
uv run kvmci create <实例名> --template Rocky-LVM   # 缺失自动下载后创建成功
rm images/<某镜像文件>
uv run kvmci create <实例名> --local           # 不触网，ImageNotFound 退出 1
# 在 images.user.conf 中定义 my-internal 镜像并被模板引用 → create 正常
# 在 images.user.conf 中同名覆盖 almalinux-10 → list 显示 user(override) 且生效
uv run kvmci images pull                       # 无参数无 --all → 退出码 2
```

- `refresh` 与工具升级后：`images.conf` 被整体重建，`images.user.conf`
  内容与用户自定义条目不受影响。
- 断网 / 全镜像不可达时：auto 缺失下载失败与 local 缺失均给出清晰
  英文错误，退出码 1；`images/` 下无半截正式镜像。
- 已存在本地镜像时 `create` 绝不触网（可用断网验证）。
- 模板写 `image_dir` 或引用不存在的镜像 ID，加载期即英文报错。
- 所有面向用户输出为英文，`grep` 不到中文提示。
