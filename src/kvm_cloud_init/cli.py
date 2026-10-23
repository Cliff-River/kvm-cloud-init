"""命令行入口：实例生命周期、查询与 cloud-init 子命令。"""

from __future__ import annotations

import argparse
import sys

import libvirt

from . import provision
from .config import ConfigStore
from .errors import KciError
from .hv.client import connect

_NAME_HELP = "instance name (the libvirt domain name)"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kvmci",
        description="Multi-template / multi-instance KVM manager based on cloud-init",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create (or recreate) an instance")
    create.add_argument("name", help=_NAME_HELP)
    create.add_argument(
        "--template",
        help="template name; overrides the instance's template reference",
    )
    create.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation for the production level "
        "(has no effect on protected)",
    )
    create.add_argument(
        "--force",
        action="store_true",
        help="force removal of a same-named production instance before recreating "
        "(equivalent to --yes for that check)",
    )
    create.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait for cloud-init to finish after creating the instance",
    )
    create.add_argument(
        "--verbose",
        action="store_true",
        help="while waiting, stream cloud-init output in a 7-line rolling area",
    )

    destroy = sub.add_parser("destroy", help="destroy an instance")
    destroy.add_argument("name", help=_NAME_HELP)
    destroy.add_argument(
        "--force",
        action="store_true",
        help="power off after the graceful shutdown timeout (otherwise abort)",
    )
    destroy.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation for the production level "
        "(has no effect on protected)",
    )

    start = sub.add_parser("start", help="start an instance")
    start.add_argument("name", help=_NAME_HELP)

    stop = sub.add_parser("stop", help="stop an instance gracefully")
    stop.add_argument("name", help=_NAME_HELP)
    stop.add_argument(
        "--force",
        action="store_true",
        help="power off if the graceful shutdown exceeds shutdown_timeout",
    )

    reboot = sub.add_parser("reboot", help="reboot a running instance")
    reboot.add_argument("name", help=_NAME_HELP)

    status = sub.add_parser("status", help="show detailed status of an instance")
    status.add_argument("name", help=_NAME_HELP)

    sub.add_parser("list", help="list configured instances and libvirt domain states")
    sub.add_parser("templates", help="list templates defined in templates.conf")

    shell = sub.add_parser("shell", help="open an interactive SSH session")
    shell.add_argument("name", help=_NAME_HELP)

    exec_cmd = sub.add_parser("exec", help="run one command inside an instance")
    exec_cmd.add_argument("name", help=_NAME_HELP)
    exec_cmd.add_argument(
        "remote_command",
        nargs=argparse.REMAINDER,
        metavar="command",
        help="remote command after '--', e.g. kvmci exec vm1 -- ls -l /",
    )

    cloud_init = sub.add_parser(
        "cloud-init", help="inspect cloud-init status and logs"
    )
    cloud_sub = cloud_init.add_subparsers(
        dest="cloud_init_command", required=True
    )
    cloud_status = cloud_sub.add_parser("status", help="show cloud-init status")
    cloud_status.add_argument("name", help=_NAME_HELP)
    cloud_log = cloud_sub.add_parser("log", help="show the cloud-init log")
    cloud_log.add_argument("name", help=_NAME_HELP)
    cloud_log.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="follow the log until cloud-init finishes",
    )
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
            _print_table(("Template", "Image", "os_variant"), rows)
            return 0

        conn = connect()

        if args.command == "create":
            return provision.create_instance(
                store,
                conn,
                args.name,
                args.template,
                yes=args.yes,
                force=args.force,
                no_wait=args.no_wait,
                verbose=args.verbose,
            )
        if args.command == "destroy":
            provision.destroy_instance(
                store, conn, args.name, force=args.force, yes=args.yes
            )
        elif args.command == "start":
            provision.start_instance(store, conn, args.name)
        elif args.command == "stop":
            provision.stop_instance(store, conn, args.name, force=args.force)
        elif args.command == "reboot":
            provision.reboot_instance(store, conn, args.name)
        elif args.command == "status":
            provision.status_instance(store, conn, args.name)
        elif args.command == "list":
            rows = provision.list_instances(store, conn)
            _print_table(("Name", "Template", "State"), rows)
        elif args.command == "shell":
            return provision.shell_instance(store, conn, args.name)
        elif args.command == "exec":
            command = list(args.remote_command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                print(
                    "Error: exec requires a remote command after '--', "
                    "e.g. kvmci exec vm1 -- ls -l /",
                    file=sys.stderr,
                )
                return 2
            return provision.exec_instance(store, conn, args.name, command)
        elif args.command == "cloud-init":
            if args.cloud_init_command == "status":
                return provision.cloud_init_status(conn, args.name)
            return provision.cloud_init_log(
                conn, args.name, follow=args.follow
            )
        return 0

    except KciError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except libvirt.libvirtError as exc:
        print(f"Error: libvirt operation failed: {exc}", file=sys.stderr)
        return 1
