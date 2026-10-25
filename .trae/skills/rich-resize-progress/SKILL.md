---
name: rich-resize-progress
description: Build rich terminal progress bars without artifacts on resize. Use when adding a rich Progress/Live bar or fixing reflow ghost lines after TTY resize. Not for GUI/web or non-rich renderers.
---

# Rich 进度条缩放自愈

用 rich `Progress`/`Live` 做 TTY 进度条，并解决终端缩放（尤其缩窄）后上方
残留旧帧快照的问题。完整可复制实现见 `assets/resize_aware_live.py`；
PTY 验证方法与排错见 `references/resize-testing.md`。

## 适用与不适用

- 适用：Python + rich 的下载/长任务进度条，要求保留 rich 视觉效果
  （彩条、已下载/总量、速度、ETA）且缩放终端不留残影。
- 不适用：GUI/Web 进度条；自绘（非 rich）渲染器；日志流里不需要就地
  刷新的场景（直接定期 print 即可）。

## 根因：rich 的相对定位在缩窄时失算

rich `Live` 每帧渲染后，`LiveRender._shape` 记录上一帧的 `(width, height)`
（旧宽度下的**逻辑行数**）。下一帧先按 `height - 1` 次 `CURSOR_UP` 回到
渲染起点再覆盖：

1. 终端**缩窄**：已输出的物理行被终端 reflow 成更多视觉行，rich 仍按旧
   `height` 上移 → 光标回不到真正的起点，旧快照残留在上方。
2. 终端**变宽**：rich 逻辑行之间是显式 `\n`，终端不会把两行合并成一行，
   原生定位仍然正确——**变宽不需要任何介入**。
3. 不要自己处理 SIGWINCH：rich 自动刷新线程默认 10Hz，缩放后最迟 0.1s
   下一帧就会触发修复逻辑。

## 实现步骤

1. 子类化 `rich.live.Live`，只重写 `process_renderables`：
   - 读 `self._live_render._shape`；为 `None`（首帧）直接放行。
   - 仅当 `self.console.is_interactive` 且 `0 < new_width < old_width` 时介入。
   - 用**旧宽度**重新布局当前帧，逐行估算在新宽度下 reflow 的视觉行数：
     `console.options.update(width=old_width)` → `console.render_lines(...)`
     → 对每行 `ceil(Segment.get_line_length(line) / new_width)` 求和。
   - 视觉行数与旧 height 不同，就在 renderables 头部插入擦除控制序列，
     并把 `live_render._shape = None`（让父类跳过按旧 height 的相对上移），
     再 `return super().process_renderables(renderables)`。
2. 擦除序列的上移次数是 **`rows - 1`，不是 `rows`**。reflow 后光标停在
   内容区最后一行的行尾：先 `CARRIAGE_RETURN` + `ERASE_IN_LINE(2)` 清当前
   行，再上移 `rows - 1` 次、每次清行，恰好覆盖 `rows` 个视觉行并停在
   首行行首。多上移一次会抹掉进度区上方已保留的普通输出（如下载提示行）。
3. 构造 `rich.progress.Progress(...)` 后、`start()` 前，把内部 live 换成
   子类实例，参数（console、refresh_per_second、transient、redirect_*、
   `get_renderable=progress.get_renderable`）与 Progress 内部构造保持一致。
4. 总量未知（chunked 响应、HEAD 不可用）时子类化 `DownloadColumn`：
   `task.total is None` 只渲染 `filesize.decimal(completed)`，避免出现
   rich 默认的 `? MB`。
5. `close()` 只调 `progress.stop()`，**绝不要先把 completed 补成 total**——
   取消下载（Ctrl+C）时最终一帧必须停在当前进度。stop 自带最后一帧刷新。
6. 非交互终端（重定向/CI）走降级路径：按固定间隔（如 5 秒）追加
   `current / total (pct%)` 文本行，用 `stream.isatty()` 判定，不要依赖
   `Console.is_terminal`（可注入的假流更容易测）。
7. Ctrl+C 行为放在调用层：取消时删除半成品文件（如 `.part`）、输出
   `Operation cancelled.`、退出码 130；进度对象本身不感知取消语义。

## 交付前检查

- 缩窄 hook 只在 `new_width < old_width` 触发；变宽走 rich 原生路径。
- 擦除上移次数 = 视觉行数 − 1；`_shape` 置空后父类不再二次上移。
- 首帧（`_shape is None`）与非交互终端不触发 hook。
- close 幂等：重复调用、close 后再 update 都安全。
- 保留 rich 原有重定向开关（`redirect_stdout/stderr=False`），避免吞掉
  业务线程的 print。
- 单测见 `references/resize-testing.md`：必须覆盖缩窄、变宽、首帧、
  未知总量、close 不补满。

## 固有边界（如实告知用户）

终端模拟器若在物理缩放后、reflow 完成前才读到一帧旧宽度输出（写入端
无法感知的极小调度窗口），该帧可能越过估算区域；下一帧（≤0.1s）恢复
正常。这是所有相对定位 TUI（含原生 rich）的共同边界，不是本方案缺陷。

## 已证伪的岔路（不要重走）

1. **自绘 `\r` + `\x1b[2K` + SIGWINCH 单行渲染**：能修残影但失去 rich
   视觉效果（彩条/速度/ETA/宽度自适应），用户明确不接受降级。
2. **DECSC/DECRC（`\x1b7`/`\x1b8`）绝对锚定**：DECSC 保存的是绝对行号，
   reflow 后内容下移而保存点不动，回锚会误擦上方 header；变宽时还残留
   1 列。相对清除才是正确模型。
3. **SIGWINCH 触发立即重绘**：无必要且更脆——rich 10Hz 刷新线程本身就
   会在缩放后下一帧调到 `process_renderables`，信号处理反而引入与刷新
   线程的竞态。
