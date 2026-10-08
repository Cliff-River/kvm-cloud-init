"""命令行入口：create / destroy / list / templates 四个子命令。"""

from __future__ import annotations

import argparse
import sys

import libvirt

from . import provision
from .config import ConfigStore
from .errors import KciError
from .hv.client import connect


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kvm-cloud-init",
        description="基于 cloud-init 的多模板 / 多实例 KVM 虚拟机管理工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="创建（或重建）实例")
    create.add_argument("name", help="实例名（即 libvirt domain 名）")
    create.add_argument(
        "--template",
        help="模板名；实例已在 instances.conf 登记时可用于覆盖其模板引用",
    )
    create.add_argument(
        "--yes",
        action="store_true",
        help="跳过 production 保护级别的交互式确认（对 protected 无效）",
    )
    create.add_argument(
        "--force",
        action="store_true",
        help="同名旧实例为 production 时强制删除后重建（等价于 --yes）",
    )

    destroy = sub.add_parser("destroy", help="销毁实例")
    destroy.add_argument("name", help="实例名")
    destroy.add_argument(
        "--force",
        action="store_true",
        help="优雅关机超时后强制断电（默认超时则中止）",
    )
    destroy.add_argument(
        "--yes",
        action="store_true",
        help="跳过 production 保护级别的交互式确认（对 protected 无效）",
    )

    sub.add_parser("list", help="列出已登记实例及 libvirt 中的域状态")
    sub.add_parser("templates", help="列出 templates.conf 中定义的模板")
    return parser


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max(_display_width(str(row[i])) for row in (header, *rows))
        for i in range(len(header))
    ]
    print("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(header)))
    for row in rows:
        print("  ".join(_pad(row[i], widths[i]) for i in range(len(header))))


def _display_width(text: str) -> int:
    """中文全角字符占两列。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(value: object, width: int) -> str:
    text = str(value)
    return text + " " * (width - _display_width(text))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        store = ConfigStore.load()

        if args.command == "templates":
            rows = provision.list_templates(store)
            _print_table(("模板名", "镜像", "os_variant"), rows)
            return 0

        conn = connect()

        if args.command == "create":
            provision.create_instance(
                store,
                conn,
                args.name,
                args.template,
                yes=args.yes,
                force=args.force,
            )
        elif args.command == "destroy":
            provision.destroy_instance(
                store, conn, args.name, force=args.force, yes=args.yes
            )
        elif args.command == "list":
            rows = provision.list_instances(store, conn)
            _print_table(("实例名", "模板", "状态"), rows)
        return 0

    except KciError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except libvirt.libvirtError as exc:
        print(f"错误：libvirt 操作失败：{exc}", file=sys.stderr)
        return 1
