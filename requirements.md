# 需求：实例生命周期命令与 cloud-init 感知

> 本文是给实现者的需求说明。按“通用约定 → 子命令 → 等待 cloud-init →
> cloud-init 子命令 → 实现约束 → 文档 → 验收”的顺序阅读，逐项落实。

## 一、目标与范围

新增实例生命周期命令 `start` / `stop` / `reboot` / `status`；
`create` 创建后默认等待 `cloud-init` 完成；新增 `cloud-init` 子命令；
统一英文提示并同步文档。

**非目标（不要做）**：
- 不新增配置字段，不改 `default.conf` / `templates.conf` / `instances.conf` 结构。
- 不改 cloud-init 文档渲染与整块替换语义。
- 不引入 subprocess 调用 `virsh` / `ssh` / `cloud-init` 等外部命令。
- 不改动已有的 SSH 密钥策略与 IP 探测优先级（lease → agent → arp）。
- 不为 `start` / `reboot` 增加“等待 cloud-init”行为（仅 `create` 等待）。

## 二、通用约定

1. **命令语法**：`kvmci <子命令> <实例名> [选项]`；实例名即 libvirt domain 名。
2. **实例解析**：
   - 已登记实例：用 `ConfigStore.resolve()` 获取模板、存储池、`shutdown_timeout`。
   - 未登记但 libvirt 中存在同名域：按默认值操作（同现有 `destroy` 行为），
     模板列显示 `-`。
   - libvirt 中不存在该域：输出英文错误并以退出码 1 结束，不做隐式创建。
3. **退出码**：`0` 成功；`1` 业务失败；`2` 参数错误。
   `list` / `templates` 等只读命令沿用现有约定。
4. **英文输出**：所有面向用户的文本必须是英文，包括 argparse 的
   `description/help`、`print` 提示、警告、错误消息、表格表头与状态名。
   代码注释与 docstring 可保持中文。
   需要同步翻译的现有位置至少包括：
   - `cli.py`：子命令 help、表格表头、错误前缀（`错误：` → `Error: `）；
   - `provision.py`：创建/销毁/警告等全部 `print`；
   - `errors.py`：所有异常 message 与 docstring 中的面向用户文案；
   - `hv/client.py`、`hv/domains.py`、`hv/storage.py` 等模块内的用户可见日志。
5. **幂等**：`start` / `stop` 对已处于目标状态（运行中 / 已关机）的实例
   不报错，打印提示后返回 0。
6. **实现分层**：`cli.py` 只做参数解析、输出与退出码转换；
   业务编排放 `provision.py`；libvirt 操作放 `hv/`；不得越层。

## 三、子命令规格

### 3.1 `start`

```
kvmci start <实例名>
```

- 域不存在 → 报错，退出码 1。
- 已运行 → 打印 `Instance <name> is already running.`，退出码 0。
- 已关机 → 调用 `dom.create(0)`，成功后打印 `Instance <name> started.`。
- 其他状态（如 paused）→ 尝试 `create(0)`，失败时把 libvirt 错误转成
  `KciError`，退出码 1。
- 不等待 IP / SSH / cloud-init。

### 3.2 `stop`

```
kvmci stop <实例名> [--force]
```

- 已关机 → 打印 `Instance <name> is already shut off.`，退出码 0。
- 运行中 → 发送 ACPI 关机（`dom.shutdown()`），按 `shutdown_timeout`
  轮询至 `shut off`；每 5 秒播报一次等待进度。
- `--force` 语义：**不跳过优雅关机**；仅在超过 `shutdown_timeout`
  仍未关闭时执行强制断电（`dom.destroy()`）。
  这与现有 `destroy --force` 语义保持一致。
- 超时且未加 `--force` → 抛 `ShutdownTimeout`，退出码 1。
- 只关机，不 undefine、不删除存储卷与密钥。
- 输出 `Instance <name> stopped.`。

### 3.3 `reboot`

```
kvmci reboot <实例名>
```

- 已运行 → 调用 `dom.reboot(0)`（ACPI reboot），打印
  `Instance <name> is rebooting.`。
- 已关机 / 非运行状态 → 报错
  `Instance <name> is not running; use 'kvmci start <name>' first.`，退出码 1。
- 不重建 domain、不改存储、不等待 cloud-init。
- reboot 失败（如客户机不支持 ACPI）→ 转为 `KciError`，退出码 1。

### 3.4 `status`

```
kvmci status <实例名>
```

- 只读命令，输出键值行（英文固定标签，冒号对齐）：

```
Name:     <实例名>
Template: <模板名或 ->
State:    <running / shut off / paused / crashed / ...>
vCPUs:    <vcpus>
Memory:   <memory> MiB
IPv4:     <地址或 ->
```

- `IPv4` 仅在运行中尝试解析：复用 `hv/network.find_ipv4()`；
  解析失败（`NetworkError`）时显示 `-`，**不得**让 `status` 整体失败。
- 未登记实例的 `Template` 显示 `-`；域不存在 → 退出码 1。
- 不修改任何状态。

### 3.5 `destroy` 复用关机逻辑

- 把现有 `hv/domains.py` 中“优雅关机 + 超时轮询 + 可选强制断电”
  抽成独立函数（如 `stop_domain(conn, name, timeout, force)`），
  供 `stop` 子命令与 `destroy` 共同调用。
- `destroy` 流程保持不变：
  保护级别校验 → 关机 → 收集/删除快照 → undefine → 清理残留 → 删除密钥。
- **禁止存在第二套关机轮询实现**。
- `destroy` 自行打印最终结果，避免复用 `stop` 时出现重复提示。

## 四、`create` 等待 cloud-init

```
kvmci create <实例名> [--template 模板] [--yes] [--force] [--no-wait] [--verbose]
```

1. `define + start` 成功后，**默认等待 cloud-init 完成**；
   `--no-wait` 立即返回 0，不等待。
2. 等待方式：
   - 复用实例元数据中的 `ssh-user`、`keys/<实例名>` 私钥；
   - 实例刚启动时 IP/SSH 可能暂不可用，需轮询重试（间隔 2 秒）；
     连续 300 秒仍无法建立 SSH 连接 → 报错退出 1，并提示可用 `--no-wait`。
   - 通过 SSH 执行 `cloud-init status --format=json`，用 `json.loads`
     结构化解析（禁止正则解析），每 2 秒轮询一次。
   - `status` 为 `running` / `not started` → 继续等待；
     `done` → 成功；`error` → 失败。
   - 等待过程不得静默：进入等待时打印一次提示；SSH 首次可达时打印
     `Connected to <name> at <ip>.`。
   - TTY：等待期间用 `⠋⠙⠹…` 转圈动画在同一行原地刷新（每 0.1 秒），
     并显示状态与耗时；SSH 未就绪时显示 `SSH: waiting for connection`，
     连上后显示 `Cloud-init status: <status> (<n>s elapsed)`。
   - 非 TTY：不做动画，每 15 秒逐行追加一次状态心跳。
   - verbose 的 TTY 滚动区域已在标题行加入转圈动画，不再重复心跳。
3. 成功：打印 `Cloud-init completed successfully.`（可附耗时），退出码 0。
4. 失败：
   - 向 stderr 打印 `Cloud-init failed:`；
   - 输出 `cloud-init status --long` 的错误摘要；
   - 输出 `/var/log/cloud-init-output.log` 末尾 20 行；
   - 退出码 1；实例保持运行，不回滚、不销毁。
5. `Ctrl+C`：
   - 捕获 `KeyboardInterrupt`，停止等待；
   - 打印 `Stopped waiting for cloud-init. Instance <name> keeps running.`；
   - 不得打印 traceback，不得中断终端；
   - 退出码 0（实例已创建成功）。
6. `--verbose`：
   - 等待期间以**最多 7 行**的固定区域滚动刷新：
     1 行状态标题 + 最近 6 行 cloud-init 输出；
   - 使用 ANSI 光标上移 / 回车重绘，结束时清除该区域；
   - 刷新间隔 1 秒；
   - 输出被重定向（非 TTY）时退化为追加打印，不输出 ANSI 控制符。
7. 无固定总超时；用户可随时 `Ctrl+C` 退出等待。

## 五、`cloud-init` 子命令

```
kvmci cloud-init status <实例名>
kvmci cloud-init log <实例名> [-f | --follow]
```

- `status`：
  - 实例必须处于运行状态，否则报错退出 1；
  - 执行 `cloud-init status --long` 并输出结果（含错误摘要）；
  - 退出码：`done` → 0；`running` / `not started` → 2；`error` → 1；
    查询本身失败 → 1。
- `log`：
  - 默认输出 `/var/log/cloud-init.log` 全部内容后退出 0；
  - `-f` / `--follow`：实时流式跟随该日志，直到 cloud-init 状态变为
    `done` 或 `error` 后退出（`done` → 0，`error` → 1）；
  - 若开始时 cloud-init 已结束，直接打印完整日志并退出，不要挂起；
  - 输出被重定向时同样按行流式输出，不缓冲到结束。
- 两个子命令均通过 paramiko 在远端执行，禁止本地 subprocess。

## 六、实现约束与测试

1. **模块拆分与结构重构**：
   - 若单个 Python 文件过于臃肿（例如超过约 400 行，或同时承担
     参数解析、业务编排、libvirt 操作、输出格式化等 3 类以上职责），
     必须拆分为多个模块或子包，不要继续堆叠在同一个文件里。
   - 允许并鼓励**适度重构**现有结构，使各功能边界更清晰，例如把
     `provision.py` 拆成 `provision/` 包，按 create、lifecycle、
     cloud_init_wait 等职责分文件；`cli.py` 可按子命令拆成
     `cli/` 包，但需保持 `kvmci` 入口与现有公开行为不变。
   - 拆分以“高内聚、低耦合”为准，不为了拆而拆；不得跨层放置逻辑
     （`cli` 不做业务，`hv` 不依赖 `provision`）。
   - 重构不得改变现有命令的行为与输出语义（除本需求要求的英文提示外）；
     相关 pytest 用例必须继续通过。
2. 新增业务函数放在 `provision` 层（若拆包则放在对应子模块并从包入口导出）：
   `start_instance` / `stop_instance` / `reboot_instance` /
   `status_instance` / `wait_cloud_init` / `cloud_init_status` /
   `cloud_init_log`。
3. `hv/domains.py` 负责关机与状态查询的 libvirt 细节；
   `hv/network.py` 继续负责 IP；`ssh.py` 负责远程命令执行。
   若现有 `run_command` 不满足流式输出，可在 `ssh.py` 增加流式接口，
   但不得调用系统 `ssh`。
4. `errors.py` 新增 cloud-init 相关异常（如 `CloudInitError`），
   继承 `KciError`，message 为英文。
5. 新增 pytest 用例（全部 mock，不连真实 libvirt / 不启动虚机）：
   - `start`：不存在 / 已运行 / 已关机；
   - `stop`：已关机 / 优雅关机成功 / 超时报错 / `--force` 强制断电；
   - `reboot`：运行中 / 未运行；
   - `status`：已登记 / 未登记 / 域不存在 / IP 解析失败降级；
   - `destroy` 确实调用共享的停止函数且不重复实现轮询；
   - 等待：成功、失败、`Ctrl+C`（`KeyboardInterrupt`）、`--no-wait`、
     非 TTY 的 verbose 降级；
   - `cloud-init status` 三种状态与退出码；`cloud-init log` 默认与 `-f`。
6. 完成后 `uv run pytest` 全绿，Ruff 无告警。

## 七、文档同步

1. `README.md`（中英双语）：
   - CLI 表格新增 `start` / `stop` / `reboot` / `status` / `cloud-init`；
   - `create` 说明补充 `--no-wait`、`--verbose` 与默认等待 cloud-init；
   - 补充 cloud-init 等待失败、`Ctrl+C`、退出码说明；
   - 快速开始示例加入 `status` 与 `cloud-init status`。
2. `AGENTS.md`：更新目录说明与常用命令；若调整了源码结构，
   同步更新其中的目录树与模块职责描述。
3. `.trae/rules/project_rules.md`：
   - 命令列表补充新子命令；
   - 将“异常 message 必须是中文”改为英文；
   - 补充“create 默认等待 cloud-init、`stop` 与 `destroy` 共用关机逻辑”。

## 八、验收标准

依次执行并核对：

```bash
uv run kvmci start  <实例名>        # 已关机 → 启动；重复执行提示 already running，退出 0
uv run kvmci status <实例名>        # 输出 Name/Template/State/vCPUs/Memory/IPv4
uv run kvmci reboot <实例名>        # 运行中重启；关机状态报错退出 1
uv run kvmci stop   <实例名>        # 优雅关机；已关机提示 already shut off，退出 0
uv run kvmci stop   <实例名> --force  # 超时后强制断电，语义与 destroy --force 一致
uv run kvmci destroy <实例名>       # 复用同一关机逻辑，流程与现状一致
uv run kvmci create <实例名>        # 默认等待 cloud-init；成功后退出 0
uv run kvmci create <实例名> --no-wait   # 不等待，立即退出 0
uv run kvmci create <实例名> --verbose   # 7 行内滚动刷新
uv run kvmci cloud-init status <实例名>
uv run kvmci cloud-init log <实例名> -f
```

- 等待期间 `Ctrl+C` 可立即退出且实例继续运行。
- cloud-init 失败时打印错误摘要与日志尾部，退出码 1。
- 所有命令输出均为英文，`grep` 不到面向用户的中文提示。
