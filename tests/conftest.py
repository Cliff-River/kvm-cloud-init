"""测试公共夹具与 qcow2 头构造辅助。"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kvm_cloud_init.config import DOC_NAMES, ConfigStore

_DEFAULT_CONF = """\
storage_pool: default
image_dir: images
memory: 1024
vcpus: 2
network: default
# firmware 保留 auto
firmware: auto
shutdown_timeout: 5
"""

_TEMPLATES_CONF = """\
templates:
  t1:
    path: templates/t1
    image_dir: images
    image: base.qcow2
    os_variant: rocky10

  t2:
    path: templates/t2
    image: u.qcow2
    os_variant: ubuntu26.04
    memory: 4096
    capacity: 50G
    user-data: |
      #cloud-config
      template_inline: true

  t3:
    image: x.qcow2
    user-data: |
      #cloud-config
      fully_inline: true
    meta-data: |
      instance-id: t3
      local-hostname: t3
"""

_INSTANCES_CONF = """\
instances:
  i1:
    template: t1
    memory: 2048

  i2:
    template: t2

  i3:
    template: t1
    capacity: 100G
    user-data: |
      #cloud-config
      instance_block: true
"""


def write_qcow2(path: Path, virtual_size: int, backing: str | None = None) -> Path:
    """写入仅包含有效头部的最小 qcow2 文件（含可选 backing file）。"""
    backing_bytes = backing.encode("utf-8") if backing else b""
    backing_offset = 104 if backing_bytes else 0
    header = bytearray(104)
    header[0:4] = b"QFI\xfb"
    struct.pack_into(">I", header, 4, 3)
    struct.pack_into(">Q", header, 8, backing_offset)
    struct.pack_into(">I", header, 16, len(backing_bytes))
    struct.pack_into(">Q", header, 24, virtual_size)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header)
        if backing_bytes:
            fh.write(backing_bytes)
    return path


@pytest.fixture
def make_qcow2():
    return write_qcow2


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """构造一个加载即可用的最小项目目录。"""
    (tmp_path / "images").mkdir()
    (tmp_path / "templates/t1").mkdir(parents=True)
    (tmp_path / "templates/t2").mkdir(parents=True)
    for name in DOC_NAMES:
        (tmp_path / "templates/t1" / name).write_text(f"{name}: from-t1-file\n")
    # t2 只有 meta-data 文件，user-data 走模板内联
    (tmp_path / "templates/t2/meta-data").write_text("instance-id: t2\n")

    (tmp_path / "default.conf").write_text(_DEFAULT_CONF)
    (tmp_path / "templates.conf").write_text(_TEMPLATES_CONF)
    (tmp_path / "instances.conf").write_text(_INSTANCES_CONF)
    return tmp_path


@pytest.fixture
def store(project_dir: Path) -> ConfigStore:
    return ConfigStore.load(project_dir)
