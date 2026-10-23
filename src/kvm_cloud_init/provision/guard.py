"""删除保护级别把关（destroy 与 create 的隐式销毁共用）。"""

from __future__ import annotations

import sys

from ..errors import ProtectionDenied


def prompt_yes(prompt: str) -> bool:
    """production 级别的交互确认：仅精确的 ``yes`` 视为确认。

    非 TTY 环境（CI / 管道 EOF）安全失败，返回 False，避免脚本误删。
    """
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


def ensure_destroy_allowed(
    name: str, level: str, *, skip_confirm: bool, action: str
) -> None:
    """销毁 / 隐式销毁前按删除保护级别把关。

    - normal：直接放行；
    - production：skip_confirm（--yes / create 的 --force）放行，
      否则交互输入 yes 才继续，未确认即抛 ProtectionDenied；
    - protected：任何标志都无法绕过，直接抛 ProtectionDenied。
    """
    if level == "normal":
        return
    if level == "protected":
        raise ProtectionDenied(
            f"Instance {name} has protection level 'protected'; {action} was refused. "
            "This level cannot be bypassed with --yes/--force. "
            "Edit the config file (remove the level field or set it back to normal) "
            "and retry."
        )
    if skip_confirm:
        return
    prompt = (
        f"Instance {name} has protection level 'production'; {action} cannot be undone.\n"
        f"Type 'yes' to confirm {action}: "
    )
    if prompt_yes(prompt):
        return
    if sys.stdin.isatty():
        raise ProtectionDenied(f"No 'yes' confirmation received; {action} cancelled.")
    raise ProtectionDenied(
        f"Instance {name} has protection level 'production' and this is a "
        f"non-interactive environment; {action} aborted. "
        "Pass --yes to confirm."
    )
