"""配置加载、校验与三级合并测试。"""

from __future__ import annotations

import pytest

from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ConfigError, InstanceNotFound, TemplateNotFound


def test_load_real_project() -> None:
    """当前仓库的真实配置必须可正常加载。"""
    store = ConfigStore.load()
    assert {"Rocky-LLM", "Ubuntu", "Debian", "AlmaLinux"} <= set(store.templates)
    assert "rocky-llm" in store.instances
    assert store.defaults.shutdown_timeout == 120


def test_merge_instance_over_template_over_defaults(store: ConfigStore) -> None:
    i1 = store.resolve("i1")
    assert i1.memory == 2048          # 实例覆盖模板/默认
    assert i1.vcpus == 2              # 默认值
    assert i1.capacity is None        # 都没设
    assert i1.template.name == "t1"

    i2 = store.resolve("i2")
    assert i2.memory == 4096          # 模板值
    assert i2.capacity == "50G"       # 模板值
    assert i2.template.image == "u.qcow2"

    i3 = store.resolve("i3")
    assert i3.memory == 1024          # 默认
    assert i3.capacity == "100G"      # 实例覆盖模板（t1 本无 capacity）


def test_ad_hoc_instance_requires_template(store: ConfigStore) -> None:
    resolved = store.resolve("anything", template_name="t1")
    assert resolved.name == "anything"
    with pytest.raises(InstanceNotFound):
        store.resolve("not-registered")


def test_missing_template_reference(project_dir) -> None:
    (project_dir / "instances.conf").write_text(
        "instances:\n  bad:\n    template: ghost\n"
    )
    with pytest.raises(ConfigError, match="模板 ghost"):
        ConfigStore.load(project_dir)


def test_cli_template_override(store: ConfigStore) -> None:
    # 已登记实例也可用 --template 覆盖引用
    resolved = store.resolve("i1", template_name="t2")
    assert resolved.template.name == "t2"


def test_unknown_template_name(store: ConfigStore) -> None:
    with pytest.raises(TemplateNotFound):
        store.resolve("i1", template_name="nope")


def test_unknown_field_rejected(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "templates:\n  bad:\n    image: x.qcow2\n    bogus: 1\n"
    )
    with pytest.raises(ConfigError, match="未知字段"):
        ConfigStore.load(project_dir)


def test_template_requires_path_or_inline(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "templates:\n  bad:\n    image: x.qcow2\n"
    )
    with pytest.raises(ConfigError, match="path 或内联"):
        ConfigStore.load(project_dir)


def test_bad_firmware(project_dir) -> None:
    (project_dir / "default.conf").write_text("firmware: weird\n")
    with pytest.raises(ConfigError, match="firmware"):
        ConfigStore.load(project_dir)


def test_image_path_resolution(store: ConfigStore) -> None:
    resolved = store.resolve("i1")
    assert resolved.image_path.name == "base.qcow2"
    assert resolved.image_path.is_absolute()
