# 需求：为虚拟机提供 SSH 交互与命令执行（`kvmci shell` / `kvmci exec`）

## 1. 背景与目标

`kvm-cloud-init` 已能把 cloud qcow2 镜像 + cloud-init NoCloud 种子交付为 KVM 虚拟机，
现有子命令为 `create / destroy / list / templates`。本需求在既有能力之上新增两个
子命令，让用户无需手工查 IP、手工配密钥即可进入虚拟机：

- `kvmci shell <实例名>`：以 SSH 打开一个交互式登录会话。
- `kvmci exec <实例名> -- <命令...>`：在虚拟机内非交互地执行一条命令并回传结果。

## 2. 术语与前置约定

- **实例名**：即 libvirt domain 名，也是 `instances.conf` 中的键；`shell` / `exec`
  按此名定位域与密钥。
- **合并后 user-data**：指 `cloudinit.render_docs` 按“实例内联 > 模板内联 > 模板 path
  目录文件”合并后的**最终成品**。本需求的“运行时注入”只作用于它，不触碰
  `templates.conf`、`instances.conf` 与模板目录中的源文件。
- **连接三元组**：目标地址 = 实测 IP（见 R5），用户 = 判定出的登录用户（见 R4），
  身份 = 程序生成的私钥（见 R1）。
- **程序生成密钥**：指 `kvmci create` 按 R1 约定生成并落盘的 `keys/<实例名>` 与
  `keys/<实例名>.pub`，其生命周期由 `kvmci` 管理（创建时生成、`destroy` 时清理）。

## 3. 功能需求

### R1 SSH 密钥生成与存储

1. 使用 `paramiko` 生成 SSH 密钥对（这是本需求唯一新增的第三方依赖），
   **不得**调用 `ssh-keygen` 或新增任何 subprocess。
2. 粒度为**每实例一对密钥**：
   - 私钥：`keys/<实例名>`
   - 公钥：`keys/<实例名>.pub`
3. 密钥类型首选 Ed25519；若运行环境不支持，回退为 RSA（密钥长度 ≥ 3072）。
4. 文件权限：私钥 `0600`、公钥 `0644`；`keys/` 不存在时自动创建（含父目录）。
5. 生成须**幂等**：密钥对已存在则复用、不重新生成、不覆盖；仅在缺失时生成。
6. `keys/` 已在 `.gitignore` 中，密钥不得提交入库。

### R2 公钥运行时注入（不写入模板）

1. 注入发生在 `create` 流程**运行时**：以 R1 的公钥，对**合并后 user-data** 做加工，
   再用于生成 `cidata.iso`。
2. 注入方式：用 PyYAML 解析合并后 user-data（`#cloud-config`），把程序公钥**追加**到
   目标用户的 `ssh_authorized_keys`，再序列化回 `#cloud-config`。序列化后必须保留
   `#cloud-config` 头及原有全部字段语义。
3. 明确**禁止**把公钥提前硬编码进模板文件或 `.conf`；也禁止改写模板目录下的文件。
4. 若合并后 user-data 无法解析为 cloud-config（缺少 `#cloud-config` 头或顶层不是映射），
   抛中文错误并中止创建，**不得静默跳过注入**。

### R3 与用户自定义公钥共存

1. 若用户在模板/实例的 user-data 中已为登录用户定义了 `ssh_authorized_keys`，
   程序公钥只能**追加**，绝不能删除或覆盖用户已有的任何公钥。
2. 连接时**只使用**程序生成的私钥 `keys/<实例名>`；不得使用用户自定义的公钥建立连接，
   不得回退到密码登录。

### R4 登录用户判定

1. 解析**合并后 user-data** 的 `users` 列表，取**第一个 `name != root` 的用户**
   作为 SSH 登录用户。
2. 若其中不存在非 root 用户，则在运行时注入阶段新增用户 `cliff` 供 SSH 使用：
   `groups: [sudo]`、sudo 免密、`lock_passwd: false`、`shell: /bin/bash`，并把程序公钥
   写入其 `ssh_authorized_keys`。该新增仅作用于运行时 user-data，不写回模板。
3. `shell` 与 `exec` 均以该登录用户身份运行远端命令。

### R5 实测 IP 获取（不信任 network-config）

1. 通过 libvirt 域接口地址 API 获取（即 `virsh domifaddr` 等价能力），按 source **顺序回退**：
   1) `lease`（DHCP 租约）→ 2) `agent`（qemu-guest-agent）→ 3) `arp`（主动探测）。
   这是唯一被信任的地址来源。
2. **忽略 `network-config` 中声明的静态地址**：虚拟机可能并未拿到该 IP。
3. 过滤规则：仅取 IPv4、排除回环；返回首个可用地址。
4. 虚拟机启动后 IP 不会立即就绪，须**带超时轮询**（缺省 60 秒、每 2 秒一次）；
   超时抛中文错误，并提示排查 DHCP、qemu-guest-agent 与网络配置。
5. 若域不存在或未处于运行状态，直接抛中文错误，**不自动启动**实例。

### R6 `kvmci shell <实例名>`

1. 以 R5 的实测 IP、R4 的登录用户、R1 的私钥、22 端口建立 SSH 会话。
2. 分配 PTY，交互式转发 stdin/stdout/stderr，用户可正常运行 `exit`、Ctrl+C 等。
3. 主机密钥采用自动接受策略（`AutoAddPolicy`）以避免首次连接阻塞（适用于本地临时实例）。
4. 会话结束后以远端退出码返回；缺少私钥、无可用 IP、连接失败等一律抛中文错误并以退出码 1 结束。

### R7 `kvmci exec <实例名> -- <命令...>`

1. 语法以 `--` 为界：`--` 之后的**所有** token 拼成一条命令，交由远端登录 shell
   （`/bin/sh -c`）执行，以支持管道与重定向。
2. 非交互执行，不分配 PTY；实时透传 stdout/stderr。
3. 以**远端退出码**作为 `kvmci` 退出码（例如 `-- sh -c 'exit 3'` 时 `kvmci` 退出码为 3）。
4. `--` 之后为空（未提供命令）时，按参数错误给出中文提示。

### R8 `kvmci destroy` 同步清理程序生成的密钥

1. 执行 `kvmci destroy <实例名>` 时，在实例销毁流程完成后，**一并删除**该实例的程序生成密钥
   `keys/<实例名>` 与 `keys/<实例名>.pub`；即使 libvirt 中已无该域，只要保护校验通过也同样清理。
2. 密钥清理必须发生在**删除保护校验通过之后**：若 `level` 为 `protected`，或 `production`
   未获确认（未 `--yes` / 非 TTY 未确认），则实例与密钥都**不删除**，直接抛 `ProtectionDenied`。
3. 清理范围限定为 R1 命名约定的两个文件，不触碰 `keys/` 下的其他文件；文件不存在时跳过，不报错。
4. 密钥删除失败（如权限不足）不得阻断实例销毁流程，但需输出中文警告，提示用户手工核对与清理。
5. `kvmci create` 对同名旧实例的**隐式重建不删除**密钥，遵循 R1 幂等语义：继续复用同一密钥对，
   使重建后的实例仍可用原私钥连接。

## 4. 实现约束（架构与工程约定）

1. **分层不变**：`cli.py` 仅做 argparse、输出与“异常 → 退出码”；SSH 相关逻辑放入独立模块
   （建议 `src/kvm_cloud_init/ssh.py`），依赖 libvirt 的 IP 查询放入 `hv/` 子包
   （建议 `hv/network.py`），编排入口在 `provision.py`（如 `shell_instance` / `exec_instance`）。
2. 所有 libvirt 交互仍只连 `qemu:///system`，禁止 `qemu:///session`。
3. 禁止新增 subprocess 包装 `ssh` / `ssh-keygen` / `virsh`；结构化解析（user-data 用 PyYAML，
   libvirt 数据用其 API/XML），禁止正则抠取。
4. 所有面向用户的错误继承 `errors.KciError`，message 为可直接展示的中文；`exec` / `shell`
   的远端命令失败应保留远端退出码而非统一成 1。
5. 全部代码加类型注解；新增依赖 `paramiko` 需同步 `pyproject.toml`。
6. 本需求默认**不新增配置字段**；若确需引入可配置项（如超时），必须经 `config.py` 校验与
   三级合并，并同步 `default.conf` / README（中英双语）与本文件。
7. 测试放入 `tests/`，用 mock/fake 覆盖；**禁止**在单测中建立真实 libvirt 或 SSH 连接。
   涉及密钥/实例删除的改动（R8）必须有对应用例：保护级别拒绝时不得删密钥、`protected` 不得被绕过、
   `create` 重建不复用删除动作。

## 5. 验收标准（可验证）

1. 执行 `uv run kvmci create <实例名>` 后：`keys/<实例名>` 与 `keys/<实例名>.pub` 均存在，
   私钥权限为 `0600`；cidata 中的 user-data 含程序公钥。
2. 模板已定义非 root 用户时，注入后该用户仍唯一存在，且程序公钥被追加到其
   `ssh_authorized_keys`；模板未定义非 root 用户时，注入后新增 `cliff`（sudo 组）。
3. 用户已在模板中定义公钥时，注入后**新旧公钥并存**，用户公钥未被删除或覆盖。
4. `uv run kvmci shell <实例名>` 能打开交互式会话，`exit` 后退出码为 0。
5. `uv run kvmci exec <实例名> -- id` 输出远端执行结果；`-- sh -c 'exit 3'` 时 `kvmci` 退出码为 3。
6. 当 `network-config` 的静态 IP 与虚拟机实际地址不一致时，仍能按实测 IP 连接成功。
7. 无可用 IP / 域未运行 / 私钥缺失 / 连接失败时，输出中文错误、退出码 1，且不出现交互式卡死。
8. 对 `level` 为 `normal` / 已确认 `production` 的实例执行 `uv run kvmci destroy <实例名>` 后，
   `keys/<实例名>` 与 `keys/<实例名>.pub` 一并消失；而 `protected` 或未确认的 `production`
   被拒绝时，实例与密钥均保持不变。

## 6. 非目标

- 不改变 `create / destroy / list / templates` 的既有 CLI 参数与核心语义（`destroy` 仅按 R8
  新增密钥清理这一副作用）。
- 不实现端口转发、`scp`、密钥自动轮换，也不新增登录用户覆盖参数（登录用户按 R4 判定）。
- 不改变 cloud-init 文档“整份文件替换、不做 YAML 深合并”的既有语义：注入是叠加在
  合并成品之上的运行时加工。
