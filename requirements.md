# 需求：实例删除保护级别（protection level）

为实例引入可配置的删除保护级别，避免生产环境实例被误删。保护级别通过配置文件定义，destroy 与 create 的隐式销毁均遵守该规则。

## 1. 字段定义

- **字段名**：`level`
- **取值**：`normal` / `production` / `protected`
- **默认值**：`normal`（未配置时即按现状可直接删除）
- **定义位置**：沿用项目现有三级合并约定，三处均可设
  - `default.conf`：全局默认值
  - `templates.conf`：模板级覆盖（适合把某类 OS 镜像整体设为 production/protected）
  - `instances.conf`：实例级覆盖（最高优先级）
- **合并优先级**：实例 > 模板 > default.conf，与现有字段一致
- **未登记实例**（`create --template` 临时创建、destroy 同名域不在 instances.conf）：无 level 配置，按 `normal` 处理

## 2. 各级别行为

| level | destroy 行为 | create 检测到同名域存在时 |
|---|---|---|
| `normal` | 直接删除（维持现状） | 隐式销毁后重建（维持现状） |
| `production` | 交互式确认，输入 `yes` 才继续；加 `--yes` 跳过确认 | 交互式确认，或加 `--yes` 跳过确认，或加 `--force` 强制删除旧实例后重建 |
| `protected` | 直接拒绝并退出，提示需改配置（删字段或改回 normal）后重试 | 直接拒绝并退出，提示同上 |

说明：
- `protected` 在任何情况下都不被 `--yes` / `--force` 绕过，唯一解除方式是修改配置文件。
- `production` 的交互确认仅对该级别生效；`normal` 不弹确认，`protected` 直接拒绝。
- 交互确认在非 TTY 环境（如 CI / 管道输入 EOF）应安全失败：视为未确认，按拒绝处理并给出明确错误，避免脚本误删。

## 3. CLI 参数变更

- `create <实例名> [--template T] [--yes] [--force]`
- `destroy <实例名> [--force] [--yes]`

参数语义：
- `--yes`：跳过 `production` 级别的交互式确认（对 `normal` 无影响、对 `protected` 无效）
- `--force`：
  - **destroy**：维持现有语义——优雅关机超时后强制断电。本需求不改变其原有含义。
  - **create**：新增语义——当同名旧实例为 `production` 时，强制删除旧实例后重建（等价于跳过 production 确认）。

> 待确认点：destroy 的 `--force`（强制断电）与 create 的 `--force`（强制删 production 旧实例）语义不同。是否需要为 destroy 也增加「`--force` 一并绕过 production 确认」的统一语义，留待下一步决定。当前按各自独立语义实现。

## 4. 代码改动范围

1. **`config.py`**
   - `Defaults` / `Template` / `InstanceSpec` / `ResolvedInstance` 增加 `level: str` 字段
   - 新增常量 `_VALID_LEVEL = ("normal", "production", "protected")`
   - 新增校验：`_choice(raw["level"], _VALID_LEVEL, ...)`，大小写归一化（与现有 graphics/video 一致）
   - `resolve()` 中按三级合并取最终 level（实例 > 模板 > 默认）
   - 未知字段校验白名单加入 `level`
2. **`provision.py`**
   - `destroy_instance`：销毁前检查 level；`protected` 抛错；`production` 且未给 `yes` 时走交互确认
   - `create_instance`：在隐式销毁同名域前检查 level；`protected` 抛错；`production` 按 `yes` / `force` 决定是否继续
   - 交互确认逻辑抽成内部函数（便于测试 mock stdin）
3. **`cli.py`**
   - `create` 子命令新增 `--yes`、`--force` 参数并向下传递
   - `destroy` 子命令新增 `--yes` 参数并向下传递（`--force` 已存在）
4. **配置文件**
   - `default.conf`：注释中补充 `level` 字段说明（不在文件中写默认值，保持 `normal` 为隐式默认）
   - `templates.conf`：注释中补充 `level` 字段说明
   - `instances.conf`：注释中补充 `level` 字段说明，并给出 production / protected 示例（注释形式）
5. **测试 `tests/`**
   - `test_config.py`：level 校验（非法值报错）、三级合并（实例覆盖模板覆盖默认）、未登记实例按 normal
   - `test_cli.py`：`create --yes` / `create --force` / `destroy --yes` 参数透传
   - `test_provision`（新增或并入现有）：destroy 对 protected 抛错、production 在未确认时拒绝、production 在 `--yes` 时通过、create 隐式销毁对 protected 抛错、create 隐式销毁 production 在 `--force` 时通过；交互确认用 monkeypatch mock stdin，禁止依赖真实 TTY
6. **文档**
   - `README.md`（中英双语）：配置字段说明、CLI 用法、各级别行为表
   - `AGENTS.md` / `.trae/rules/project_rules.md`：同步 `level` 字段说明

## 5. 不在本次范围

- 不改变 `--force` 在 destroy 中的现有「强制断电」语义
- 不引入新的配置文件位置或绕过 `config.py` 校验的旁路
- 不对 cloud-init 文档做 YAML 深合并
- 不改动存储卷命名规则与 libvirt 连接方式（仍 `qemu:///system`）

## 6. 验收

- `uv run pytest` 全绿，新增用例覆盖三级合并、各级别 destroy/create 行为、参数透传
- `uv run kvm-cloud-init templates|list` 不受影响
- 手动验证：production 实例 destroy 弹确认、protected 实例 destroy/create 被拒、`--yes` / `--force` 行为符合上表
- README / AGENTS.md / project_rules.md / 三个 .conf 注释与实现一致
