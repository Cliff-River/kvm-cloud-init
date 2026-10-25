"""配置加载、校验与三级合并测试。"""

from __future__ import annotations

import pytest

from kvm_cloud_init.config import ConfigStore
from kvm_cloud_init.errors import ConfigError, InstanceNotFound, TemplateNotFound


def test_load_real_project() -> None:
    """当前仓库的真实配置必须可正常加载。"""
    store = ConfigStore.load()
    assert {"Rocky-LVM", "Ubuntu", "Debian", "AlmaLinux"} <= set(store.templates)
    assert "rocky-lvm" in store.instances
    assert store.defaults.shutdown_timeout == 120


def test_merge_instance_over_template_over_defaults(store: ConfigStore) -> None:
    i1 = store.resolve("i1")
    assert i1.memory == 2048          # 实例覆盖模板/默认
    assert i1.vcpus == 2              # 默认值
    assert i1.capacity is None        # 都没设
    assert i1.template.name == "t1"
    assert i1.graphics == "vnc"       # 默认值
    assert i1.video == "auto"         # 默认值

    i2 = store.resolve("i2")
    assert i2.memory == 4096          # 模板值
    assert i2.capacity == "50G"       # 模板值
    assert i2.graphics == "spice"     # 模板覆盖默认
    assert i2.video == "virtio"
    assert i2.template.image == "ubuntu"

    i3 = store.resolve("i3")
    assert i3.memory == 1024          # 默认
    assert i3.capacity == "100G"      # 实例覆盖模板（t1 本无 capacity）
    assert i3.graphics == "none"      # 实例覆盖


def test_ad_hoc_instance_requires_template(store: ConfigStore) -> None:
    resolved = store.resolve("anything", template_name="t1")
    assert resolved.name == "anything"
    with pytest.raises(InstanceNotFound):
        store.resolve("not-registered")


def test_missing_template_reference(project_dir) -> None:
    (project_dir / "instances.conf").write_text(
        "bad:\n  template: ghost\n"
    )
    with pytest.raises(ConfigError, match="references template ghost"):
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
        "bad:\n  image: x.qcow2\n  bogus: 1\n"
    )
    with pytest.raises(ConfigError, match="Unknown fields"):
        ConfigStore.load(project_dir)


def test_template_requires_path_or_inline(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "bad:\n  image: x.qcow2\n"
    )
    with pytest.raises(ConfigError, match="path or inline"):
        ConfigStore.load(project_dir)


@pytest.mark.parametrize(
    ("filename", "wrapper"),
    [("templates.conf", "templates"), ("instances.conf", "instances")],
)
def test_legacy_wrapper_key_rejected(project_dir, filename: str, wrapper: str) -> None:
    (project_dir / filename).write_text(f"{wrapper}:\n  x: {{}}\n")
    with pytest.raises(ConfigError, match="deprecated top-level key"):
        ConfigStore.load(project_dir)


def test_bad_firmware(project_dir) -> None:
    (project_dir / "default.conf").write_text("firmware: weird\n")
    with pytest.raises(ConfigError, match="firmware"):
        ConfigStore.load(project_dir)


def test_bad_graphics_in_defaults(project_dir) -> None:
    (project_dir / "default.conf").write_text("graphics: rdp\n")
    with pytest.raises(ConfigError, match="graphics"):
        ConfigStore.load(project_dir)


def test_bad_video_in_template(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "t1:\n  path: templates/t1\n  image: b.qcow2\n  video: svga\n"
    )
    with pytest.raises(ConfigError, match="video"):
        ConfigStore.load(project_dir)


def test_image_path_resolution(store: ConfigStore) -> None:
    resolved = store.resolve("i1")
    assert resolved.image_path.name == "base.qcow2"
    assert resolved.image_path.is_absolute()


def test_level_defaults_to_normal(store: ConfigStore) -> None:
    # 已登记实例未配置 level
    assert store.resolve("i1").level == "normal"
    # 未登记的临时实例同样按 normal
    assert store.resolve("adhoc", template_name="t1").level == "normal"


def test_level_three_way_merge(project_dir) -> None:
    (project_dir / "default.conf").write_text(
        "storage_pool: default\n"
        "image_dir: images\n"
        "memory: 1024\n"
        "vcpus: 2\n"
        "network: default\n"
        "firmware: auto\n"
        "level: production\n"
        "shutdown_timeout: 5\n"
    )
    (project_dir / "templates.conf").write_text(
        "t1:\n  path: templates/t1\n  image: base\n  level: protected\n"
        "t2:\n  path: templates/t2\n  image: ubuntu\n"
    )
    (project_dir / "instances.conf").write_text(
        # 实例/模板都没设 -> default.conf 的 production
        "from-default:\n  template: t2\n"
        # 模板设 protected，实例未覆盖
        "from-template:\n  template: t1\n"
        # 实例 normal 覆盖模板 protected
        "from-instance:\n  template: t1\n  level: normal\n"
    )
    store = ConfigStore.load(project_dir)
    assert store.resolve("from-default").level == "production"
    assert store.resolve("from-template").level == "protected"
    assert store.resolve("from-instance").level == "normal"
    # 未登记实例：模板 t1 为 protected
    assert store.resolve("adhoc", template_name="t1").level == "protected"


def test_level_case_normalized(project_dir) -> None:
    (project_dir / "instances.conf").write_text(
        "upper:\n  template: t1\n  level: PRODUCTION\n"
    )
    store = ConfigStore.load(project_dir)
    assert store.resolve("upper").level == "production"


@pytest.mark.parametrize(
    ("filename", "body", "match"),
    [
        ("default.conf", "level: strict\n", "level"),
        (
            "templates.conf",
            "t1:\n  path: templates/t1\n  image: b.qcow2\n  level: nope\n",
            "level",
        ),
        (
            "instances.conf",
            "i1:\n  template: t1\n  level: nope\n",
            "level",
        ),
    ],
)
def test_bad_level_rejected(project_dir, filename: str, body: str, match: str) -> None:
    (project_dir / filename).write_text(body)
    with pytest.raises(ConfigError, match=match):
        ConfigStore.load(project_dir)


# ---- 镜像目录（images.conf / images.user.conf） ----


def test_missing_images_conf_is_generated(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "t:\n  path: templates/t1\n  image: almalinux-10\n"
    )
    (project_dir / "instances.conf").write_text("")
    (project_dir / "images.conf").unlink()
    store = ConfigStore.load(project_dir)
    assert (project_dir / "images.conf").exists()
    assert "almalinux-10" in store.images
    assert store.images["rocky-10-lvm"].origin == "system"


def test_user_images_new_and_override(project_dir) -> None:
    (project_dir / "images.user.conf").write_text(
        "internal:\n"
        "  file: internal.qcow2\n"
        "  kind: url\n"
        "  urls: [https://internal/internal.qcow2]\n"
        "  checksum_url: https://internal/internals.qcow2.sha256\n"
        "base:\n"
        "  file: base-alt.qcow2\n"
        "  kind: url\n"
        "  urls: [https://internal/base.qcow2]\n"
        "  checksum_url: https://internal/base.qcow2.sha256\n"
    )
    store = ConfigStore.load(project_dir)
    assert store.images["internal"].origin == "user"
    assert store.images["base"].origin == "user(override)"
    assert store.image_path("base").name == "base-alt.qcow2"


def test_template_image_id_must_exist(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "bad:\n  path: templates/t1\n  image: ghost\n"
    )
    (project_dir / "instances.conf").write_text("")
    with pytest.raises(ConfigError, match="references image ghost"):
        ConfigStore.load(project_dir)


def test_template_image_dir_rejected(project_dir) -> None:
    (project_dir / "templates.conf").write_text(
        "bad:\n  path: templates/t1\n  image: base\n  image_dir: images\n"
    )
    with pytest.raises(ConfigError, match="Unknown fields.*image_dir"):
        ConfigStore.load(project_dir)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("bad:\n  kind: url\n", "missing required field file"),
        ("bad:\n  file: b.qcow2\n  kind: weird\n", "field kind"),
        ("bad:\n  file: b.qcow2\n  kind: url\n  urls: []\n  checksum_url: https://x/b.sha256\n", "non-empty list"),
        ("bad:\n  file: b.qcow2\n  kind: index\n  pages: [https://x/]\n  match: '['\n  checksum_file: CHECKSUM\n", "not a valid regular expression"),
        ("bad:\n  file: b.qcow2\n  kind: url\n  urls: [https://x/b.qcow2]\n  checksum_url: https://x/b.sha256\n  checksum_algo: md5\n", "checksum_algo"),
        ("bad:\n  file: b.qcow2\n  kind: url\n  urls: [https://x/b.qcow2]\n", "no checksum source"),
        ("base2:\n  file: base.qcow2\n  kind: url\n  urls: [https://x/base.qcow2]\n  checksum_url: https://x/base.sha256\n", "must be unique"),
        ("base:\n  file: base.qcow2\n  kind: url\n  urls: [https://x/base.qcow2]\n  checksum_url: https://x/base.sha256\n  insecure: maybe\n", "must be a boolean"),
    ],
)
def test_image_source_validation(project_dir, body: str, match: str) -> None:
    (project_dir / "images.user.conf").write_text(body)
    with pytest.raises(ConfigError, match=match):
        ConfigStore.load(project_dir)


def test_bad_image_policy(project_dir) -> None:
    (project_dir / "default.conf").write_text("image_policy: offline\n")
    with pytest.raises(ConfigError, match="image_policy"):
        ConfigStore.load(project_dir)


def test_image_policy_defaults_to_auto(store: ConfigStore) -> None:
    assert store.defaults.image_policy == "auto"
