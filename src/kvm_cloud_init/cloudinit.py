"""cloud-init 文档渲染与落盘。

每份文档（meta-data / network-config / user-data）的取值优先级：
实例内联 > 模板内联 > 模板 ``path`` 目录下的同名文件。
内联覆盖以"整份文档"为粒度，不做 YAML 深合并。
"""

from __future__ import annotations

from pathlib import Path

from .config import DOC_NAMES, REQUIRED_DOCS, ConfigStore, ResolvedInstance
from .errors import ConfigError


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
                f"模板 {tpl.name} 无法提供 {name}："
                f"请在 path 目录放置该文件，或在模板/实例中内联"
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
