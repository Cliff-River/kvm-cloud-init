"""cloud-init 文档渲染与落盘。

每份文档（meta-data / network-config / user-data）的取值优先级：
实例内联 > 模板内联 > 模板 ``path`` 目录下的同名文件。
内联覆盖以"整份文档"为粒度，不做 YAML 深合并。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import DOC_NAMES, REQUIRED_DOCS, ConfigStore, ResolvedInstance
from .errors import ConfigError

#: cloud-config 文档固定头
CLOUD_CONFIG_HEADER = "#cloud-config"
#: 模板未定义非 root 用户时，运行时新增的 SSH 登录用户
DEFAULT_SSH_USER = "cliff"


def render_docs(store: ConfigStore, resolved: ResolvedInstance) -> dict[str, str]:
    """返回最终写入 cidata 的三份 cloud-init 文档内容。"""
    tpl = resolved.template
    inst = resolved.instance
    docs: dict[str, str] = {}

    for name in DOC_NAMES:
        if inst is not None and name in inst.docs:
            docs[name] = inst.docs[name]
        elif name in tpl.docs:
            docs[name] = tpl.docs[name]
        elif tpl.path:
            file_path = store.root / tpl.path / name
            if file_path.is_file():
                docs[name] = file_path.read_text(encoding="utf-8")

    for name in REQUIRED_DOCS:
        if not docs.get(name, "").strip():
            raise ConfigError(
                f"Template {tpl.name} cannot provide {name}: "
                "add the file under its path directory or inline it in the template/instance"
            )
    return docs


def write_docs(docs: dict[str, str], directory: Path) -> dict[str, Path]:
    """把文档写入指定目录，返回 {文档名: 文件路径}。"""
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, content in docs.items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written[name] = path
    return written


def _load_cloud_config(user_data: str, context: str) -> dict:
    """把合并后 user-data 解析为 cloud-config 映射。"""
    if not user_data.lstrip().startswith(CLOUD_CONFIG_HEADER):
        raise ConfigError(
            f"{context} is not a #cloud-config document; cannot inject the SSH public key. "
            "Convert user-data to cloud-config format and retry"
        )
    try:
        data = yaml.safe_load(user_data)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{context} user-data is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{context} user-data top-level structure must be a mapping")
    return data


def inject_ssh_access(user_data: str, public_key: str) -> tuple[str, str]:
    """运行时把公钥注入合并后 user-data，返回 (新文档, 登录用户名)。

    - 选用 ``users`` 中第一个非 root 用户；不存在则新增 ``cliff``（sudo 组）。
    - 公钥**追加**到该用户的 ``ssh_authorized_keys``，不覆盖既有公钥。
    """
    data = _load_cloud_config(user_data, "Instance")
    users = data.get("users")
    if users is None:
        users = []
        data["users"] = users
    if not isinstance(users, list):
        raise ConfigError("user-data users must be a list")

    target: dict | None = None
    login_user: str | None = None
    for entry in users:
        if isinstance(entry, dict) and str(entry.get("name", "")) != "root":
            target = entry
            login_user = str(entry["name"])
            break

    if target is None:
        login_user = DEFAULT_SSH_USER
        target = {
            "name": DEFAULT_SSH_USER,
            "groups": ["sudo"],
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "lock_passwd": False,
            "shell": "/bin/bash",
        }
        users.append(target)

    authorized = target.setdefault("ssh_authorized_keys", [])
    if not isinstance(authorized, list):
        raise ConfigError(
            f"User {login_user} ssh_authorized_keys must be a list"
        )
    if public_key not in authorized:
        authorized.append(public_key)

    body = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"{CLOUD_CONFIG_HEADER}\n{body}", login_user
