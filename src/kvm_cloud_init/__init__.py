"""kvm-cloud-init：多模板 / 多实例 KVM 虚拟机管理工具。"""

# 先定义版本号：imagefetch 在 cli 导入链上会读取它，避免循环导入。
__version__ = "0.1.0"

from .cli import main

__all__ = ["main"]
