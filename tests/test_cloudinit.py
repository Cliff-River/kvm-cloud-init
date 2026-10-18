"""cloud-init 文档渲染优先级测试。"""

from __future__ import annotations

import pytest
import yaml

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


# ---- 运行时公钥 / 登录用户注入 ----


def test_inject_adds_cliff_when_no_non_root_user() -> None:
    doc = "#cloud-config\nssh_pwauth: true\n"
    new, user = cloudinit.inject_ssh_access(doc, "ssh-rsa KCI kci")
    assert user == "cliff"
    assert new.startswith("#cloud-config")
    data = yaml.safe_load(new)
    assert data["ssh_pwauth"] is True
    assert [u["name"] for u in data["users"]] == ["cliff"]
    assert data["users"][0]["ssh_authorized_keys"] == ["ssh-rsa KCI kci"]
    assert "sudo" in data["users"][0]["groups"]


def test_inject_skips_root_and_picks_first_non_root_user() -> None:
    doc = "#cloud-config\nusers:\n  - name: root\n  - name: cliff\n"
    new, user = cloudinit.inject_ssh_access(doc, "ssh-rsa KCI kci")
    assert user == "cliff"
    data = yaml.safe_load(new)
    assert [u["name"] for u in data["users"]] == ["root", "cliff"]
    assert data["users"][1]["ssh_authorized_keys"] == ["ssh-rsa KCI kci"]


def test_inject_appends_without_overriding_user_keys() -> None:
    doc = (
        "#cloud-config\n"
        "users:\n"
        "  - name: cliff\n"
        "    ssh_authorized_keys:\n"
        "      - ssh-rsa USERKEY user@example\n"
    )
    new, user = cloudinit.inject_ssh_access(doc, "ssh-rsa KCI kci")
    assert user == "cliff"
    keys = yaml.safe_load(new)["users"][0]["ssh_authorized_keys"]
    assert keys == ["ssh-rsa USERKEY user@example", "ssh-rsa KCI kci"]


def test_inject_is_idempotent() -> None:
    doc = "#cloud-config\nusers:\n  - name: cliff\n"
    once, _ = cloudinit.inject_ssh_access(doc, "ssh-rsa KCI kci")
    twice, _ = cloudinit.inject_ssh_access(once, "ssh-rsa KCI kci")
    assert yaml.safe_load(twice)["users"][0]["ssh_authorized_keys"] == ["ssh-rsa KCI kci"]


def test_inject_rejects_non_cloud_config() -> None:
    with pytest.raises(ConfigError, match="cloud-config"):
        cloudinit.inject_ssh_access("#!/bin/bash\necho hi\n", "ssh-rsa KCI kci")
