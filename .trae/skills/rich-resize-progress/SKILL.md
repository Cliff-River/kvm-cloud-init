---
name: rich-resize-progress
description: Build polished rich terminal progress bars with adaptive columns, unknown totals, non-TTY fallback, safe close, and resize recovery. Use for Python CLI progress. Not for GUI, web, or non-rich renderers.
---

# Rich 美观终端进度条

用 rich `Progress`/`Live` 构建下载或长任务进度条：TTY 下提供清晰、宽度自适应
的一帧式界面，非 TTY 下输出可读日志，并正确处理未知总量、取消和终端缩放。
完整可复制实现见 `assets/download_progress.py`；测试与排错见
`references/resize-testing.md`。

## 1. 先定义简单的表现层接口

- 对业务层只暴露 `update(done, total)` 与 `close()`；`total=None` 表示总量未知。
  下载、重试、清理和取消语义留在业务层，进度对象只负责显示。
- 业务编排通过工厂注入进度实现，避免核心下载逻辑直接依赖 rich。
- 构造函数接收可选 `stream`，缺省用 `sys.stdout`；用 `stream.isatty()` 选择
  TTY 或降级路径。可注入流比依赖全局 `Console.is_terminal` 更容易测试。
- 首次 `update` 再创建 rich 对象；保存 task ID，后续只更新同一 task。
- 用 `_closed` 保证 close 幂等，并让 close 后的 update 安全忽略。

## 2. 组合美观且自适应的 TTY 界面

按“标签 → 进度条 → 已下载/总量 → 速度 → ETA”的顺序配置列：

- `TextColumn("[bold]{task.description}")`：突出任务名。
- `BarColumn(bar_width=None)`：让彩条占据剩余宽度，终端变宽变窄都能自适应。
- `DownloadColumn` 的定制子类：显示字节进度，并处理未知总量。
- `TransferSpeedColumn()` 与 `TimeRemainingColumn()`：提供速度和剩余时间。

用 `Console(file=stream)` 构造 `Progress`，保留 `auto_refresh=True`、
`transient=False`、`redirect_stdout=False`、`redirect_stderr=False`：
自动刷新负责动画，非 transient 保留最终帧，关闭重定向避免吞掉业务输出。
`start()` 后只 `add_task` 一次，之后用 `update(completed=done, total=total)`。

## 3. 未知总量也要显示得完整

chunked 响应或 HEAD 不可用时会得到 `total=None`。子类化 `DownloadColumn`，
此时只渲染 `filesize.decimal(int(task.completed))`，否则交给父类；不要显示
rich 默认的 `? MB`，也不要伪造总量、百分比或 ETA。

## 4. 非 TTY 降级为节流日志

重定向和 CI 不输出控制序列，改为追加文本行：

- 用固定间隔（如 5 秒）节流，格式为 `current / total (pct%)` 并 `flush=True`。
- 首条有效进度和完成行必须输出；未知总量且 `done == 0` 时不打印空进度。
- 未知总量只显示已下载字节；完成行不受节流间隔抑制。
- 保持单位口径明确：rich 默认用十进制 MB，自定义纯文本可用 MiB；测试不要
  混淆两种单位。

## 5. 结束与取消不能伪造 100%

`close()` 只调用 `progress.stop()`，由 stop 刷新最后一帧。**绝不要先把
completed 补成 total**：取消时最终帧必须停在实际进度。重复 close、close 后
update 都应安全。Ctrl+C 的半成品清理、提示和退出码由调用层处理。

## 6. 终端缩窄时清除 reflow 残影

美观不仅来自列配置，也来自缩放后的稳定性。rich 用 `LiveRender._shape` 中
旧帧的 `(width, height)` 做相对定位；缩窄后终端把旧物理行 reflow 成更多视觉行，
rich 仍按旧 height 上移，旧快照便残留在上方。变宽不会合并显式换行分隔的
逻辑行，因此无需介入。

1. 子类化 `rich.live.Live`，只重写 `process_renderables`：首帧 `_shape is None`
   直接放行；仅在交互终端且 `0 < new_width < old_width` 时处理。
2. 用旧宽度重新布局当前帧：`console.options.update(width=old_width)` →
   `console.render_lines(...)`，再对每行按新宽度求
   `ceil(Segment.get_line_length(line) / new_width)` 并求和。
3. 视觉行数变化时，在 renderables 头部插入擦除序列，并把
   `live_render._shape = None`，防止父类再按旧 height 上移，最后调用父类。
4. 擦除时先 `CARRIAGE_RETURN` + `ERASE_IN_LINE(2)` 清当前行，再上移
   **`rows - 1` 次**并逐行清除。多上移一次会抹掉进度区上方的普通输出。
5. 构造 `Progress` 后、`start()` 前替换内部 live；console、refresh 频率、
   transient、redirect 开关和 `get_renderable=progress.get_renderable` 与原构造
   保持一致。
6. 不处理 SIGWINCH：rich 自动刷新线程默认 10Hz，下一帧会自然触发自愈。

## 交付前检查

- TTY 同时具有任务名、彩条、字节进度、速度和 ETA，彩条随终端宽度伸缩。
- 未知总量不出现 `? MB`；非 TTY 无 ANSI 控制序列且按间隔输出。
- close 不补满、幂等，close 后 update 不出帧；业务 print 不被 rich 吞掉。
- 首帧不上移；缩窄恰好上移视觉行数 − 1 次；变宽走 rich 原生路径。
- 单测覆盖上述行为，真实残影用 PTY 验证，方法见
  `references/resize-testing.md`。

## 固有边界

终端模拟器若在物理缩放后、reflow 完成前才读到一帧旧宽度输出，该帧可能越过
估算区域；下一帧（通常不超过 0.1 秒）恢复。这是相对定位 TUI 的共同边界。

## 已证伪的岔路

1. 自绘 `\r` + 清行 + SIGWINCH：会失去 rich 的彩条、速度、ETA 和宽度自适应。
2. DECSC/DECRC 绝对锚定：reflow 后保存点不随内容移动，可能误擦上方输出。
3. SIGWINCH 立即重绘：无必要，并会引入与 rich 刷新线程的竞态。
