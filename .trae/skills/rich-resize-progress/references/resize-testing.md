# 缩放自愈的测试与排错

单测验证控制序列，PTY 端到端验证真实终端语义。两者都不能少：单测快且
确定，但 StringIO 不会 reflow；PTY 才是残影真正发生的地方。

## 1. 单测：可注入假 TTY + 可变宽度

关键坑：CI/编辑器里 `TERM=dumb` 会让 rich `console.is_interactive=False`，
hook 直接不触发。必须同时设 `TERM` 环境变量并 monkeypatch `Console.size`：

```python
import io

class _TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def _patch_console_width(monkeypatch, columns: int) -> dict[str, int]:
    from rich.console import Console as RichConsole
    from rich.console import ConsoleDimensions

    monkeypatch.setenv("TERM", "xterm-256color")
    width = {"value": columns}  # 可变宽度：改字典即可模拟缩放
    monkeypatch.setattr(
        RichConsole,
        "size",
        property(lambda self: ConsoleDimensions(width["value"], 24)),
    )
    return width
```

必备用例与断言：

- **首帧不上移**：80 列渲染单行内容，输出中没有 `\x1b[1A`（CURSOR_UP）。
- **缩窄逐行清除**：70 列内容 80→20 列后 reflow 成 4 个视觉行，hook 输出
  **恰好 3 次** `\x1b[1A`（rows−1）+ 每行 `\x1b[2K`（ERASE_IN_LINE）。
  断言 `== 3` 而不是 `>= 1`，多上移一次就是抹 header 的回归。
- **变宽走原生路径**：20→80，`\x1b[1A` 次数等于 rich 原生 height−1
  （同样 3 次，但来自父类定位而非 hook）。
- **未知总量无 `? MB`**：total=None 更新若干次后刷新，`"? MB" not in out`
  且只显示已下载字节（十进制，如 `1.0 MB`）。
- **close 不补满**：25% 进度 close，输出有当前值（`1.0/4.2 MB`）、
  没有 `4.2/4.2 MB`；close 幂等，close 后 update 不报错也不出帧。
- 普通（非 TTY）路径：节流间隔内只输出首行/完成行，文本用 MiB。

注意 rich 的十进制单位：4 MiB 显示为 `4.2 MB`，断言时按 rich 实际输出写。

## 2. PTY 端到端：单元格级终端模拟器回放

真实终端里 `ioctl(TIOCSWINSZ)` 与子进程 write 是异步的，断言"屏幕上有
几行进度"需要模拟终端的 reflow，而不是数原始字节。写一个最小模拟器：

- 屏幕模型按**单元格**存字符（宽字符占 2 列），UTF-8 用 incremental
  decoder 分片解码（子进程一次 write 可能切断多字节字符）。
- 宽度变化时整屏 reflow：硬换行处分段，软换行（行尾自动折行）处合并后
  按新宽度重排；光标按"段落 + 单元格偏移"重新锚定。
- 至少支持：`\r`、`\n`、`CSI 2K`（清行）、`CSI 1A`（上移）、
  show/hide cursor；若实验绝对定位方案还要支持 `\x1b7/\x1b8` 与 `CSI 1B`。

事件循环（**最关键的一条，决定假阳性率**）：

```
for new_width in widths:
    drain_pending()          # 非阻塞读尽 PTY 缓冲并先回放
    set_winsz(fd, new_width) # 再改窗口大小并 reflow
    time.sleep(...)          # 让 rich 自动刷新线程出修复帧
```

缩放前**必须先排空在途数据**。真实终端会持续读取 PTY，缩放瞬间缓冲区
基本是空的；若不 drain，旧宽度帧在模拟器里排队到 reflow 之后才被回放，
会人为制造"旧帧越过估算区域"的竞态，出现约 10% 的假 FAIL。

建议的缩放序列：`80→20→80→20→80` 再追加 3 次快速连续缩放；子进程每秒
出 10 帧、持续约 7 秒。每个屏幕快照断言：

- 进度文本（如 `Downloading`）在屏上只出现预期帧数（1 条 header +
  1 帧进度，即 `count == 2`），多出来的就是残影；
- 进度区**上方**的 header 行内容完整保留（验证 rows−1 没有多擦）。

调试时加 `EMU_TRACE=1` 打印每次 reflow 前后的几何（宽、视觉行数、
光标段落/偏移、控制序列）。

## 3. 用真实 PTY 抓控制序列（快速冒烟）

不写模拟器也可以先做序列级冒烟：`pty.openpty()` 启动子进程，
`fcntl` 设非阻塞读，`struct.pack("HHHH", rows, cols, 0, 0) + TIOCSWINSZ`
改尺寸，把读到的字节落盘检查。一次 80→20 的缩窄预期看到：

- 修复帧含 `rows−1` 次 `\x1b[1A` 与等量 `\x1b[2K`；
- stop 时末帧停在当前进度，随后 `\r\n\x1b[?25h`（换行并恢复光标）；
- 取消下载时末帧数字不是总量。

极窄宽度（如 20 列）下 rich 会把整行 Progress 折叠成单行省略号
（如 `Downloading almali… `，表格 ellipsize），此时视觉行数可能反而
变少，hook 正确地不插入擦除——这是预期行为，不是 bug。

## 4. 排错速查

| 现象 | 原因 |
|---|---|
| hook 从不触发 | `TERM=dumb` 致 `is_interactive=False`，或首帧 `_shape is None`（正常） |
| 上方 header 被擦掉 | 上移了 `rows` 次，应为 `rows−1` |
| 修复后仍有一行残留 | 忘了 `live_render._shape = None`，父类按旧 height 二次定位 |
| 变宽后出现错位 | hook 误在变宽分支介入；变宽必须走原生路径 |
| PTY 测试偶发 FAIL | 缩放前没 drain 在途帧，先排空再 `TIOCSWINSZ` |
| 取消时进度跳到 100% | close 前手动补了 completed=total；删掉，`stop()` 自己刷末帧 |
| 极窄窗口残影 | rich 折叠成省略号单行是预期；恢复宽度后下一帧自愈 |
