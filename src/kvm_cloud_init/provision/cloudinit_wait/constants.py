"""轮询间隔、渲染尺寸与远端命令常量。"""

from __future__ import annotations

#: 查询状态与等待 cloud-init 的轮询间隔（秒）
POLL_INTERVAL = 2
#: verbose 模式刷新间隔（秒）
VERBOSE_INTERVAL = 1
#: 默认模式进度心跳间隔（秒）
PROGRESS_INTERVAL = 15
#: TTY 转圈动画帧与刷新间隔
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_INTERVAL = 0.1
#: verbose 区域最多显示 7 行：1 行状态 + 6 行输出
VERBOSE_LINES = 6
#: 6 行输出分别取自 output 日志与 debug 日志
VERBOSE_OUTPUT_LINES = 3
VERBOSE_DEBUG_LINES = 3
ROWS = 7

STATUS_JSON_CMD = "cloud-init status --format=json"
STATUS_LONG_CMD = "cloud-init status --long"
LOG_FILE = "/var/log/cloud-init.log"
OUTPUT_LOG = "/var/log/cloud-init-output.log"
