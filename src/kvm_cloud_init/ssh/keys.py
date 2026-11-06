"""每实例 SSH 密钥对：生成、复用与删除。

密钥存放在项目根下的 ``keys/<实例名>`` 与 ``keys/<实例名>.pub``，
全部走 paramiko，不调用 ``ssh-keygen`` 子进程。
"""

from __future__ import annotations

import os
from pathlib import Path

import paramiko

from ..config import project_root
from ..errors import SshError


def keys_dir() -> Path:
    """密钥目录：项目根下的 ``keys/``（已在 .gitignore 中）。"""
    return project_root() / "keys"


def private_key_path(instance: str) -> Path:
    return keys_dir() / instance


def public_key_path(instance: str) -> Path:
    return keys_dir() / f"{instance}.pub"


def _generate_key() -> paramiko.PKey:
    """生成密钥对：首选 Ed25519，运行环境不支持时回退 RSA-3072。"""
    generator = getattr(paramiko.Ed25519Key, "generate", None)
    if generator is not None:
        return generator()
    return paramiko.RSAKey.generate(3072)


def ensure_keypair(instance: str) -> Path:
    """确保 ``keys/<实例名>`` 与 ``.pub`` 存在，返回私钥路径。

    幂等：密钥对已存在则复用，不覆盖、不重新生成。
    """
    priv = private_key_path(instance)
    pub = public_key_path(instance)
    if priv.is_file() and pub.is_file():
        return priv

    key = _generate_key()
    keys_dir().mkdir(parents=True, exist_ok=True)
    key.write_private_key_file(str(priv))
    os.chmod(priv, 0o600)
    pub.write_text(f"{key.get_name()} {key.get_base64()}\n", encoding="utf-8")
    os.chmod(pub, 0o644)
    return priv


def read_public_key(instance: str) -> str:
    """读取公钥文本（单行 OpenSSH 格式）。"""
    pub = public_key_path(instance)
    if not pub.is_file():
        raise SshError(
            f"Public key for instance {instance} does not exist: {pub}; "
            "run 'kvmci create <name>' first"
        )
    return pub.read_text(encoding="utf-8").strip()


def delete_keypair(instance: str) -> list[Path]:
    """删除程序生成的密钥对，返回实际删除的文件列表。

    仅处理 R1 命名约定的两个文件；缺失即跳过，不报错。
    """
    removed: list[Path] = []
    for path in (private_key_path(instance), public_key_path(instance)):
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed
