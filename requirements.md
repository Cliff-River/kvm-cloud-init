# 模块拆分与整合方案

> 状态：方案稿，暂未执行。
> 范围：`src/kvm_cloud_init/`。
> 原则：按职责拆包；拆不动或拆了更差的模块保持原样，不为行数强行拆分。

## 1. 现状盘点

| 模块 | 行数 | 现有职责 | 结论 |
| --- | ---: | --- | --- |
| `ssh.py` | 1113 | 密钥、连接、交互 shell、一次性命令、SFTP 上传下载、sync 镜像 | **拆为 `ssh/` 包** |
| `imagefetch.py` | 771 | HTTP、目录页解析、校验和、压缩包解压、暂存目录、下载、状态文件 | **拆为 `imagefetch/` 包** |
| `config.py` | 591 | 配置模型、YAML 读取、标量校验、镜像源解析、三级合并、镜像目录合并 | 第二阶段可选拆为 `config/` 包 |
| `provision/images.py` | 465 | 镜像目录 init/refresh、list/check/pull、ensure_image、下载落盘辅助 | 保持原样 |
| `provision/cloudinit_wait.py` | 414 | cloud-init 轮询等待、状态查询、日志跟随、终端渲染 | **拆为 `provision/cloudinit_wait/` 包** |
| `cli.py` | 391 | argparse 声明、表格输出、异常转退出码 | 保持原样 |
| `hv/domains.py` | 298 | 共享关机、快照、残留收集、undefine | 保持原样 |
| `catalog.py` | 243 | 内置预设与确定性渲染 | 保持原样 |
| `provision/progress.py` | 207 | rich 下载进度表现层 | 保持原样 |
| `provision/sync.py` | 197 | sync 编排（与 `transfer.py` 重复 `_connect_target`） | **并入 `provision/files/`** |
| `portforward.py` | 191 | paramiko direct-tcpip 本地转发 | **并入 `ssh/` 包（可选）** |
| `provision/mounts.py` | 191 | 9p 挂载编排 | 保持原样 |
| `provision/transfer.py` | 148 | cp 编排 | **并入 `provision/files/`** |
| `hv/filesystem.py` | 176 | 9p 设备 XML、attach/detach | 保持原样 |

`hv/`、`provision/` 已经是按职责拆分的包，本次只处理包内的“单文件过长”和“功能重叠”。

## 2. 判定标准

同时满足第 3、4 条，再满足第 1 或第 2 条时才拆：

1. 单文件超过 400 行；
2. 可以命名出 3 个以上互不依赖的职责段；
3. 拆出的模块之间依赖单向、无循环；
4. 公共 API 能由包门面 `__init__.py` 完整保持。

以下情况不拆：

- 单一线性流水线，拆开只会增加跨模块参数传递（`config.py`、`provision/images.py`）；
- 跨架构层（`hv/` 与 `provision/`），合并会破坏“libvirt 交互 / 业务编排”的边界；
- 声明式代码（`cli.py` 的 argparse 定义）；
- 体量已合理、职责单一的模块。

## 3. 目标结构总览

```text
src/kvm_cloud_init/
├── ssh/                      # 原 ssh.py，SSH 传输能力包
│   ├── __init__.py           # 门面，保持 kvm_cloud_init.ssh.* 公共 API
│   ├── keys.py
│   ├── client.py
│   ├── session.py
│   ├── command.py
│   ├── sftp.py
│   ├── sync.py
│   └── portforward.py        # 原顶层 portforward.py（可选）
├── imagefetch/               # 原 imagefetch.py，镜像获取能力包
│   ├── __init__.py
│   ├── models.py
│   ├── http.py
│   ├── index.py
│   ├── resolve.py
│   ├── archive.py
│   ├── staging.py
│   ├── download.py
│   └── state.py
└── provision/
    ├── files/                # 原 transfer.py + sync.py 合并
    │   ├── __init__.py
    │   ├── paths.py
    │   ├── cp.py
    │   └── sync.py
    └── cloudinit_wait/       # 原 cloudinit_wait.py
        ├── __init__.py
        ├── constants.py
        ├── query.py
        ├── render.py
        ├── wait.py
        └── log.py
```

`config/` 为可选的第二阶段，见第 8 节。

## 4. P0：`ssh.py` → `ssh/` 包

### 4.1 目标模块与迁移映射

| 新模块 | 迁入内容 | 原行号 |
| --- | --- | --- |
| `ssh/keys.py` | `keys_dir`、`private_key_path`、`public_key_path`、`_generate_key`、`ensure_keypair`、`read_public_key`、`delete_keypair` | 40–101 |
| `ssh/client.py` | `CONNECT_TIMEOUT`、`CONNECT_RETRY_INTERVAL`、`CONNECT_RETRY_TIMEOUT`、`_connect` | 30–35、104–158 |
| `ssh/session.py` | `_sync_window_size`、`interactive_shell`、`_pump_interactive`、`_pump`、`_stdio_is_tty`、`run_command`、`_drain` | 161–323 |
| `ssh/command.py` | `capture_command`、`stream_command` | 325–389 |
| `ssh/sftp.py` | `_sftp_reason`、`_local_reason`、`remote_is_directory`、`_ensure_remote_dirs`、`_remote_kind`、`_download_tree`、`_upload_tree`、`upload_path`、`download_path` | 392–736 |
| `ssh/sync.py` | `SyncStats`、sync 目标保护、`_remote_list`、`_remove_remote_tree`、`_remove_local_tree`、`_sync_dir_upload`、`_sync_dir_download`、`sync_to_remote`、`sync_from_remote` | 739–1113 |
| `ssh/__init__.py` | 门面 re-export，无业务逻辑 | — |

依赖方向（单向，无循环）：

```text
keys    -> config, errors
client  -> errors
session -> client, portforward
command -> client
sftp    -> client
sync    -> client, sftp（_sftp_reason/_local_reason/_ensure_remote_dirs/_remote_kind）
```

### 4.2 门面与 `__all__`

`ssh/__init__.py` 只做导入，不写实现。`__all__` 至少包含：

- `ensure_keypair`、`read_public_key`、`delete_keypair`、`private_key_path`、`public_key_path`、`keys_dir`
- `CONNECT_TIMEOUT`、`CONNECT_RETRY_INTERVAL`、`CONNECT_RETRY_TIMEOUT`
- `interactive_shell`、`run_command`
- `capture_command`、`stream_command`
- `remote_is_directory`、`upload_path`、`download_path`
- `SyncStats`、`sync_to_remote`、`sync_from_remote`

所有 `from .. import ssh; ssh.xxx` 调用点的改动量为零。

### 4.3 关键约定：包内调用必须经模块对象

原测试通过 `monkeypatch.setattr(ssh, "_connect", fake)` 替换连接函数。拆包后如果写成
`from .client import _connect`，`ssh._connect` 的替换不会影响其他子模块已绑定的引用。

统一写法：

```python
# ssh/sftp.py、ssh/sync.py、ssh/session.py、ssh/command.py
from . import client

client._connect(...)
```

同理，`ssh/sync.py` 对 `sftp` 的共享辅助函数也使用 `from . import sftp` +
`sftp._ensure_remote_dirs(...)`。

### 4.4 `portforward.py` 整合（可选子项）

`portforward.py` 只被 `ssh.session` 使用，属于同一条 SSH 传输能力链。建议移入
`ssh/portforward.py`，使 `ssh/` 包自包含。

影响点：

- `cli.py`：`from .portforward import parse_port_forwards` → `from .ssh.portforward import parse_port_forwards`；
- `tests/test_portforward.py`：导入路径改为 `kvm_cloud_init.ssh.portforward`；
- `ssh/__init__.py` 可顺带 re-export `PortForwarder`、`PortMapping`、`parse_port_forwards`。

如果希望保留顶层 `portforward` 作为公开模块，可跳过本子项，不影响其余拆分。

### 4.5 测试改动

| 文件 | 改动 |
| --- | --- |
| `tests/test_ssh.py` | `ssh._connect` → `ssh.client._connect`；`ssh.time`、`ssh.paramiko` → `ssh.client.*`；`ssh.select`、`ssh.os`、`ssh.sys`、`ssh.termios`、`ssh.tty`、`ssh.signal` → `ssh.session.*`；`ssh.CONNECT_RETRY_INTERVAL` → `ssh.client.CONNECT_RETRY_INTERVAL` |
| `tests/test_sftp.py` | 2 处 `setattr(ssh, "_connect", ...)` → `ssh.client._connect` |
| `tests/test_sync.py` | 1 处 `setattr(ssh, "_connect", ...)` → `ssh.client._connect` |
| `tests/test_portforward.py` | 若执行 4.4，更新导入路径 |

注意：`ssh.sys.stdin` 这类对标准库对象的 patch 作用在共享对象上，改成
`ssh.session.sys.stdin` 后语义不变；不要为了让旧测试通过而在门面里 re-export
`time`/`select` 等模块。

## 5. P1：`provision/transfer.py` + `provision/sync.py` → `provision/files/`

### 5.1 整合依据

两个模块都负责“宿主机 ↔ 实例”的文件搬运，且存在逐字重复：

- `transfer.py` 的 `_connect_target`（46–58）与 `sync.py` 的 `_connect_target`（67–79）
  完全一致；
- 两者都依赖 `access._ensure_running`、`access.wait_ssh_target`、`SSH_READY_TIMEOUT`；
- 两者都使用 `transfer.parse_remote` 与 `RemotePath`。

### 5.2 目标模块

| 新模块 | 内容 |
| --- | --- |
| `provision/files/paths.py` | `RemotePath`、`parse_remote`、`_connect_target`（去重后的唯一实现）、`_SSH_READY_TIMEOUT` |
| `provision/files/cp.py` | `copy_file`、`_upload`、`_download`、`_kind_label` |
| `provision/files/sync.py` | `sync_paths`、`_source_basename`、`_local_source_basename`、`_local_target`、`_remote_target`、`_reject_tilde_path`、`_has_empty_remote_path`、`_summary`、`_sync_up`、`_sync_down` |
| `provision/files/__init__.py` | 门面导出 `RemotePath`、`parse_remote`、`copy_file`、`sync_paths` |

`provision/__init__.py` 的公共导出保持 `copy_file`、`parse_remote`、`sync_paths` 不变；
仅把 import 来源从 `.transfer`/`.sync` 改为 `.files`。删除原 `transfer.py` 与 `sync.py`，
不保留转发 shim。

### 5.3 测试改动

| 文件 | 改动 |
| --- | --- |
| `tests/test_transfer.py` | `from kvm_cloud_init.provision import transfer` → `from kvm_cloud_init.provision.files import cp as transfer`（或直接改断言对象为 `cp.copy_file`）；`transfer.domains`、`transfer.access` 的 patch 移到 `files.paths`（`_connect_target` 所在模块） |
| `tests/test_sync.py` | `from kvm_cloud_init.provision import sync as syncmod` → `from kvm_cloud_init.provision.files import sync as syncmod`；`syncmod.domains`、`syncmod.access` 的 patch 移到 `files.paths`；`syncmod.ssh` 保持不变 |

公共 API 测试（`provision.sync_paths`、`provision.copy_file`、`provision.parse_remote`）不需要改。

## 6. P1：`imagefetch.py` → `imagefetch/` 包

### 6.1 目标模块

| 新模块 | 内容 | 原行号 |
| --- | --- | --- |
| `imagefetch/models.py` | `Progress` 协议、`RemoteImage`、`RemoteTarget`、`DownloadResult`、`_PermanentError`、`Client` 类型别名 | 77–115 |
| `imagefetch/http.py` | `TIMEOUT`、`USER_AGENT`、`new_client`、`_use_client`、`_describe`、`_check_response`、`_attempt`、`fetch_text`、`_fetch_text_one` | 118–213 |
| `imagefetch/index.py` | `_EXTENSIONS`、`extract_hrefs`、`basename`、`parse_index`、`version_of`、`pick_latest`、`mtime_of` | 214–316 |
| `imagefetch/resolve.py` | `_HASH_LENGTHS`、`_COREUTILS`、`_BSD`、`resolve_target`、`_match_checksum_name`、`parse_checksum`、`fetch_checksum` | 317–446 |
| `imagefetch/archive.py` | `ARCHIVE_SUFFIXES`、`_TAR_MODES`、`is_archive`、`extract_archive`、`_only_member`、`_write_sparse` | 447–522 |
| `imagefetch/staging.py` | `STAGING_DIRNAME`、`STAGING_LOCK`、`STAGING_MAX_AGE`、`staging_area`、`sweep_staging` | 523–587 |
| `imagefetch/download.py` | `CHUNK_SIZE`、`RETRIES`、`BACKOFF`、`download`、`_download_once`、`_ChecksumMismatch`、`_content_length`、`_remote_size`、`_unlink`、`_free_space` | 588–730 |
| `imagefetch/state.py` | `STATE_FILENAME`、`load_state`、`save_state` | 731–771 |
| `imagefetch/__init__.py` | 门面 re-export 上述公共符号 | — |

依赖方向：

```text
models  <- http, index, resolve, download
http    <- index, resolve, download
index   <- resolve
archive / staging / state 为叶子模块，由 provision/images.py 直接使用
```

`staging_area` 仍由 `provision/images.py` 使用，`download` 本身不负责暂存目录，
保持现有调用关系不变。

### 6.2 兼容性

- `provision/images.py` 使用 `from .. import catalog, imagefetch, imagefmt` 并调用
  `imagefetch.resolve_target(...)`，门面 re-export 后无需改动；
- 测试把 `images.imagefetch.*` 作为 patch 目标，实际调用方是 `provision/images.py`
  的属性查找，门面 re-export 后仍然生效；
- 包内子模块之间同样使用 `from . import x` + `x.func(...)`，避免“patch 门面不生效”。
- `tests/test_imagefetch.py` 直接使用 `from kvm_cloud_init import imagefetch` 的公共 API，
  预计无需改动；实施时用 `grep -n "setattr" tests/test_imagefetch.py` 复核。

## 7. P1：`provision/cloudinit_wait.py` → `provision/cloudinit_wait/` 包

### 7.1 目标模块

保持模块名不变（`cloudinit_wait`），只把单文件变为包，因此
`provision/__init__.py` 与测试的导入路径都不变。

| 新模块 | 内容 |
| --- | --- |
| `cloudinit_wait/constants.py` | `POLL_INTERVAL`、`VERBOSE_INTERVAL`、`PROGRESS_INTERVAL`、`SPINNER_*`、`ROWS`、`STATUS_JSON_CMD`、`STATUS_LONG_CMD`、`LOG_FILE`、`OUTPUT_LOG` |
| `cloudinit_wait/query.py` | `_capture`、`_query_state`、`_tail_lines`、`_can_read`（远端只读原语） |
| `cloudinit_wait/render.py` | `_VerboseRenderer`、`_ProgressLine`、`_print_failure` |
| `cloudinit_wait/wait.py` | `_wait_target`、`wait_cloud_init` |
| `cloudinit_wait/log.py` | `cloud_init_status`、`cloud_init_log`、`_print_log` |
| `cloudinit_wait/__init__.py` | 门面导出 3 个公共函数，并 re-export 测试与外部引用的常量（`STATUS_JSON_CMD`、`STATUS_LONG_CMD`、`LOG_FILE`、`OUTPUT_LOG`、`SPINNER_FRAMES`、`POLL_INTERVAL` 等） |

依赖方向：`query` 提供所有远端读取原语；`render`、`wait`、`log` 只依赖 `query` 与
`access`，彼此不互相导入。

`cloud_init_status` 与 `cloud_init_log` 都是只读查询，放同一模块避免过度切分。

### 7.2 测试改动

`tests/test_cloudinit_wait.py` 目前 patch `cloudinit_wait.time`、`cloudinit_wait.ssh`、
`cloudinit_wait.sys`、`cloudinit_wait.access`。拆包后改为实际持有实现的子模块：

- `cloudinit_wait.time` → `cloudinit_wait.wait.time`（等待轮询）/ `cloudinit_wait.query.time`；
- `cloudinit_wait.sys` → `cloudinit_wait.render.sys` 或 `cloudinit_wait.log.sys`；
- `cloudinit_wait.ssh.capture_command` → 保持 `cloudinit_wait.query.ssh`（`query.py` 内 `from .. import ssh`）；
- `cloudinit_wait.ssh.stream_command` → `cloudinit_wait.log.ssh`；
- `cloudinit_wait.access.ssh_target` → `cloudinit_wait.wait.access`；
- `cloudinit_wait._tail_lines` → `cloudinit_wait.query._tail_lines`（私有函数不放门面）；
- `cloudinit_wait.STATUS_JSON_CMD` 等常量继续从门面读取，无需改动。

具体以实施时 `grep -n "monkeypatch.setattr" tests/test_cloudinit_wait.py` 的逐条结果为准。

## 8. P2（可选）：`config.py` → `config/` 包

只有在后续继续膨胀或新增配置类别时才执行；当前 591 行仍属一条校验流水线。

| 新模块 | 内容 |
| --- | --- |
| `config/models.py` | `project_root`、`Defaults`、`ImageSource`、`Template`、`InstanceSpec`、`ResolvedInstance`、`DOC_NAMES`/`REQUIRED_DOCS` |
| `config/parse.py` | `_load_yaml`、`_load_table`、标量校验、`_parse_image_source`、`_parse_defaults`、`_parse_template`、`_parse_instance`、`_extract_docs` |
| `config/store.py` | `ConfigStore`、`_load_images`、`_write_catalog`、`_reject_duplicate_files` |
| `config/__init__.py` | 门面，保持 `from kvm_cloud_init.config import ...` 不变 |

`cloudinit.py` 从 `config` 导入 `DOC_NAMES`、`REQUIRED_DOCS`、`ConfigStore`、
`ResolvedInstance`，门面 re-export 后无需改动。

## 9. 明确保持原样的模块

| 模块 | 理由 |
| --- | --- |
| `cli.py` | 主要是 argparse 声明；拆分只会增加命令注册的跳转层级 |
| `provision/images.py` | 单条镜像业务链；`_pull_one`/`ensure_image` 共享下载、校验、落盘辅助，拆开需大量跨模块传参 |
| `hv/domains.py` | 域生命周期内聚（关机、快照、残留、undefine），298 行可接受 |
| `provision/mounts.py` + `hv/filesystem.py` | 分属编排层与 libvirt XML 层，合并会越层 |
| `catalog.py`、`imagefmt.py`、`qcow2.py` | 单一能力，体量合理 |
| `provision/progress.py` | 表现层独立，已与编排解耦 |
| `hv/guest.py`、`storage.py`、`network.py`、`osinfo.py`、`firmware.py` | 各自单一 libvirt 能力 |
| `cloudinit.py`、`seediso.py` | 单一渲染/生成职责 |

## 10. 全局约定

1. **门面只做 re-export**：每个包的 `__init__.py` 不写业务逻辑，显式维护 `__all__`。
2. **包内调用走模块对象**：`from . import x`，调用 `x.func(...)`；禁止用
   `from .x import func` 后跨子模块复用可被测试替换的符号。
3. **私有符号不跨包**：`_connect`、`_sftp_reason` 等只在包内共享；包外继续走门面。
4. **不保留兼容 shim**：删除旧 `ssh.py`、`imagefetch.py`、`transfer.py`、`sync.py`、
   `cloudinit_wait.py` 时不留下转发文件，避免双份 import 路径。
5. **常量归属**：连接超时归 `ssh/client.py`；cloud-init 轮询与日志路径归
   `provision/cloudinit_wait/constants.py`；暂存时长归 `imagefetch/staging.py`；
   下载重试与分块归 `imagefetch/download.py`；校验清单正则归
   `imagefetch/resolve.py`；压缩包后缀归 `imagefetch/archive.py`。
6. **无循环依赖**：每批完成后用 `python -c "import kvm_cloud_init"` 与 pytest 验证。
7. **行数目标**：拆分后单文件不超过 400 行；超过时重新评估职责，而不是继续机械切分。

## 11. 验证清单

每批改动后至少执行：

```bash
uv run pytest
uvx ruff check src tests
uv run python -c "import kvm_cloud_init; import kvm_cloud_init.ssh; import kvm_cloud_init.imagefetch; import kvm_cloud_init.provision"
uv run kvmci --help
uv run kvmci images list
```

验收标准：

- 公共 API（`kvm_cloud_init.ssh.*`、`kvm_cloud_init.imagefetch.*`、
  `kvm_cloud_init.provision.*`）名称与签名不变；
- 现有测试全部通过（允许修改 patch 目标与导入路径，不允许删减断言）；
- `ruff` 无新增告警；
- 单文件行数符合第 10 条第 7 款。

## 12. 文档同步

实施时同步以下位置：

- `AGENTS.md`：目录树中的 `ssh.py`、`imagefetch.py`、`transfer.py`、`sync.py`、
  `cloudinit_wait.py` 条目，以及“工作方式”中引用这些文件名的段落；
- `.trae/rules/project_rules.md`：架构边界中的 `ssh.py`、`imagefetch.py`、
  `provision/transfer.py`、`provision/sync.py`、`provision/cloudinit_wait.py` 描述；
- `provision/__init__.py` 顶部 docstring 的模块清单；
- 各新包的模块 docstring：说明职责与依赖方向；
- README 如出现模块级文件名，一并更新（当前检索未发现必须改动项）。

## 13. 执行顺序与分批

每批独立提交、独立验证，前一批全绿后再开始下一批：

1. **批次 A（P0）**：`ssh/` 包 + 测试 patch 路径调整；可选合并 `portforward.py`。
2. **批次 B（P1）**：`provision/files/` 合并，去掉重复的 `_connect_target`。
3. **批次 C（P1）**：`imagefetch/` 包拆分。
4. **批次 D（P1）**：`provision/cloudinit_wait/` 包拆分。
5. **批次 E（P2，可选）**：`config/` 包拆分。

批次 A 与 B 有轻微耦合（`provision/files` 仍通过 `ssh` 门面调用），但接口不变，
可独立完成。

## 14. 风险与回滚

| 风险 | 表现 | 应对 |
| --- | --- | --- |
| monkeypatch 失效 | patch 门面属性后测试仍走真实实现，出现真实网络/连接 | 严格执行第 10 条第 2 款；每批先跑对应测试文件 |
| 循环导入 | `import kvm_cloud_init` 报 `ImportError` | 子模块只依赖更底层模块；`__init__.py` 最后导入 |
| 公共 API 漏导出 | `provision` 或 `cli` 启动时报 `AttributeError` | 用第 11 节的 import 冒烟命令检查 |
| 旧文件残留 | 同时存在 `ssh.py` 与 `ssh/`，导入行为不确定 | 删除旧文件与空 `__pycache__`，确认 `git status` |
| 测试改动掩盖回归 | 改 patch 路径时误删断言 | 只改导入与 patch 目标，不改断言逻辑；逐块 diff 复核 |

回滚方式：每批一个 commit，出现问题直接 `git revert <commit>`，不影响其他批次。

## 15. 不做的事

- 不改变任何运行时行为、错误消息、CLI 输出与配置格式；
- 不引入新依赖；
- 不新增功能、不做顺手重构；
- 不把 `hv/` 与 `provision/` 合并，也不把 `provision/images.py` 移到 `imagefetch/`；
- 不为拆分而拆分：第 9 节列出的模块保持原样。
