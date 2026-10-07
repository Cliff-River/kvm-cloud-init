"""cloud-init 文档渲染优先级测试。"""

from __future__ import annotations

import pytest

from kvm_cloud_init import cloudinit
from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ConfigError


def test_docs_from_path(store: ConfigStore) -> None:
    docs = cloudinit.render_docs(store, store.resolve("i1"))
    assert docs["user-data"] == "user-data: from-t1-file\n"
    assert docs["meta-data"] == "meta-data: from-t1-file\n"
    assert "network-config" in docs


def test_template_inline_overrides_path(store: ConfigStore) -> None:
    docs = cloudinit.render_docs(store, store.resolve("i2"))
    assert "template_inline: true" in docs["user-data"]
    # 未内联的 meta-data 仍来自 path
    assert docs["meta-data"] == "instance-id: t2\n"


def test_instance_block_replaces(store: ConfigStore) -> None:
    docs = cloudinit.render_docs(store, store.resolve("i3"))
    assert "instance_block: true" in docs["user-data"]
    assert "from-t1-file" not in docs["user-data"]


def test_fully_inline_template(store: ConfigStore) -> None:
    docs = cloudinit.render_docs(store, store.resolve("adhoc", template_name="t3"))
    assert "fully_inline: true" in docs["user-data"]
    assert "local-hostname: t3" in docs["meta-data"]


def test_missing_required_doc(project_dir) -> None:
    (project_dir / "instances.conf").write_text("")
    (project_dir / "templates.conf").write_text(
        """\
empty:
  path: templates/ghost
  image: base.qcow2
"""
    )
    store = ConfigStore.load(project_dir)
    with pytest.raises(ConfigError, match="meta-data"):
        cloudinit.render_docs(store, store.resolve("x", template_name="empty"))
