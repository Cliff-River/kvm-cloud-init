"""三个 YAML 配置文件的加载、校验与优先级合并。

文件（均位于项目根目录）：
- ``default.conf``    全局默认值，最低优先级
- ``templates.conf``  OS 模板定义
- ``instances.conf``  实例定义，最高优先级

合并优先级：实例覆盖 > 模板定义 > default.conf。
cloud-init 三份文档（meta-data / network-config / user-data）只做整块替换，
不做 YAML 深合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, InstanceNotFound, TemplateNotFound

# cloud-init NoCloud 的三份文档文件名（也是模板/实例配置中的内联字段名）
DOC_NAMES = ("meta-data", "network-config", "user-data")
REQUIRED_DOCS = ("meta-data", "user-data")
_SCALAR_FIELDS = ("memory", "vcpus", "capacity")
_VALID_FIRMWARE = ("auto", "uefi", "bios")
_VALID_GRAPHICS = ("vnc", "spice", "none")
_VALID_VIDEO = ("auto", "virtio", "bochs", "cirrus", "qxl")
#: 模板/实例可覆盖的字符串型硬件选项
_CHOICE_FIELDS = ("graphics", "video")


def project_root() -> Path:
    """项目根目录：src/kvm_cloud_init/config.py 向上三级。"""
    return Path(__file__).resolve().parents[2]


@dataclass
class Defaults:
    storage_pool: str = "default"
    image_dir: str = "images"
    memory: int = 6144
    vcpus: int = 6
    network: str = "default"
    firmware: str = "auto"
    graphics: str = "vnc"
    video: str = "auto"
    shutdown_timeout: int = 120


@dataclass
class Template:
    """templates.conf 中的一个 OS 模板。"""

    name: str
    image: str
    path: str | None = None
    image_dir: str | None = None
    os_variant: str = ""
    memory: int | None = None
    vcpus: int | None = None
    capacity: str | None = None
    graphics: str | None = None
    video: str | None = None
    docs: dict[str, str] = field(default_factory=dict)


@dataclass
class InstanceSpec:
    """instances.conf 中的一个实例定义。"""

    name: str
    template: str
    memory: int | None = None
    vcpus: int | None = None
    capacity: str | None = None
    graphics: str | None = None
    video: str | None = None
    docs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedInstance:
    """实例与模板、默认值合并后的最终创建参数。"""

    name: str
    template: Template
    instance: InstanceSpec | None
    image_path: Path
    memory: int
    vcpus: int
    capacity: str | None
    network: str
    firmware: str
    graphics: str
    video: str
    shutdown_timeout: int
    storage_pool: str


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 YAML：{exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 的顶层结构必须是键值映射")
    return data


def _load_table(path: Path, legacy_wrapper: str) -> dict[str, Any]:
    """加载 templates/instances 配置：根映射即为定义表。

    检测旧版包装键（templates:/instances:）并拒绝，提示去掉一层缩进。
    """
    data = _load_yaml(path)
    if legacy_wrapper in data and isinstance(data[legacy_wrapper], dict):
        raise ConfigError(
            f"{path.name} 仍使用已废弃的顶层键 “{legacy_wrapper}:”，"
            f"请将其下的条目提升到顶层（删除该键并减少一层缩进）"
        )
    return data


def _as_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} 必须是整数，当前值：{value!r}")
    if value <= 0:
        raise ConfigError(f"{context} 必须为正整数，当前值：{value}")
    return value


def _choice(value: Any, options: tuple[str, ...], context: str) -> str:
    text = str(value).lower()
    if text not in options:
        raise ConfigError(f"{context} 必须是 {'/'.join(options)} 之一，当前值：{value!r}")
    return text


def _parse_defaults(raw: dict[str, Any]) -> Defaults:
    allowed = set(Defaults.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"default.conf 存在未知字段：{', '.join(sorted(unknown))}")
    defaults = Defaults()
    if "storage_pool" in raw:
        defaults.storage_pool = str(raw["storage_pool"])
    if "image_dir" in raw:
        defaults.image_dir = str(raw["image_dir"])
    if "memory" in raw:
        defaults.memory = _as_int(raw["memory"], "default.conf 的 memory")
    if "vcpus" in raw:
        defaults.vcpus = _as_int(raw["vcpus"], "default.conf 的 vcpus")
    if "network" in raw:
        defaults.network = str(raw["network"])
    if "firmware" in raw:
        defaults.firmware = _choice(raw["firmware"], _VALID_FIRMWARE, "default.conf 的 firmware")
    if "graphics" in raw:
        defaults.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, "default.conf 的 graphics")
    if "video" in raw:
        defaults.video = _choice(raw["video"], _VALID_VIDEO, "default.conf 的 video")
    if "shutdown_timeout" in raw:
        defaults.shutdown_timeout = _as_int(
            raw["shutdown_timeout"], "default.conf 的 shutdown_timeout"
        )
    return defaults


def _extract_docs(raw: dict[str, Any], context: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    for name in DOC_NAMES:
        if name in raw:
            value = raw[name]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"{context} 的 {name} 必须是文本块（使用 | 标量）")
            if value:
                docs[name] = value
    return docs


def _parse_template(name: str, raw: Any) -> Template:
    if not isinstance(raw, dict):
        raise ConfigError(f"模板 {name} 的定义必须是键值映射")
    if "image" not in raw or not raw["image"]:
        raise ConfigError(f"模板 {name} 缺少必填字段 image")
    tpl = Template(name=name, image=str(raw["image"]))
    if "path" in raw and raw["path"] is not None:
        tpl.path = str(raw["path"])
    if "image_dir" in raw and raw["image_dir"] is not None:
        tpl.image_dir = str(raw["image_dir"])
    if "os_variant" in raw and raw["os_variant"] is not None:
        tpl.os_variant = str(raw["os_variant"])
    if "memory" in raw and raw["memory"] is not None:
        tpl.memory = _as_int(raw["memory"], f"模板 {name} 的 memory")
    if "vcpus" in raw and raw["vcpus"] is not None:
        tpl.vcpus = _as_int(raw["vcpus"], f"模板 {name} 的 vcpus")
    if "capacity" in raw and raw["capacity"] is not None:
        tpl.capacity = str(raw["capacity"])
    if raw.get("graphics") is not None:
        tpl.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, f"模板 {name} 的 graphics")
    if raw.get("video") is not None:
        tpl.video = _choice(raw["video"], _VALID_VIDEO, f"模板 {name} 的 video")
    tpl.docs = _extract_docs(raw, f"模板 {name}")
    allowed = {
        "path", "image_dir", "image", "os_variant", "memory", "vcpus",
        "capacity", *_CHOICE_FIELDS, *DOC_NAMES,
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"模板 {name} 存在未知字段：{', '.join(sorted(unknown))}")
    if not tpl.path and not tpl.docs:
        raise ConfigError(f"模板 {name} 必须设置 path 或内联 cloud-init 字段")
    return tpl


def _parse_instance(name: str, raw: Any) -> InstanceSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"实例 {name} 的定义必须是键值映射")
    if "template" not in raw or not raw["template"]:
        raise ConfigError(f"实例 {name} 缺少必填字段 template")
    inst = InstanceSpec(name=name, template=str(raw["template"]))
    for scalar in ("memory", "vcpus"):
        if scalar in raw and raw[scalar] is not None:
            setattr(inst, scalar, _as_int(raw[scalar], f"实例 {name} 的 {scalar}"))
    if "capacity" in raw and raw["capacity"] is not None:
        inst.capacity = str(raw["capacity"])
    if raw.get("graphics") is not None:
        inst.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, f"实例 {name} 的 graphics")
    if raw.get("video") is not None:
        inst.video = _choice(raw["video"], _VALID_VIDEO, f"实例 {name} 的 video")
    inst.docs = _extract_docs(raw, f"实例 {name}")
    allowed = {"template", *_SCALAR_FIELDS, *_CHOICE_FIELDS, *DOC_NAMES}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"实例 {name} 存在未知字段：{', '.join(sorted(unknown))}")
    return inst


class ConfigStore:
    """加载并持有三份配置，提供实例解析。"""

    def __init__(
        self,
        root: Path,
        defaults: Defaults,
        templates: dict[str, Template],
        instances: dict[str, InstanceSpec],
    ) -> None:
        self.root = root
        self.defaults = defaults
        self.templates = templates
        self.instances = instances

    @classmethod
    def load(cls, root: Path | str | None = None) -> ConfigStore:
        base = Path(root) if root else project_root()
        defaults = _parse_defaults(_load_yaml(base / "default.conf"))

        # 顶层即模板/实例表（文件名已表明语义，不再使用包装键）。
        # templates / instances 为保留名，用于识别旧格式并给出迁移提示。
        templates = {
            name: _parse_template(name, body)
            for name, body in _load_table(base / "templates.conf", "templates").items()
        }
        instances = {
            name: _parse_instance(name, body)
            for name, body in _load_table(base / "instances.conf", "instances").items()
        }

        # 交叉校验：实例引用的模板必须存在
        for inst in instances.values():
            if inst.template not in templates:
                raise ConfigError(
                    f"实例 {inst.name} 引用的模板 {inst.template} 未在 templates.conf 中定义"
                )
        return cls(base, defaults, templates, instances)

    def resolve(self, name: str, template_name: str | None = None) -> ResolvedInstance:
        """合并出创建实例所需的全部标量参数。

        - 已登记实例可通过 template_name 覆盖其模板引用；
        - 未登记实例必须显式提供 template_name（临时创建）。
        """
        inst = self.instances.get(name)
        if inst is None and not template_name:
            raise InstanceNotFound(
                f"实例 {name} 未在 instances.conf 中定义；"
                f"临时创建请通过 --template 指定模板"
            )
        chosen = template_name or (inst.template if inst else None)
        if chosen not in self.templates:
            raise TemplateNotFound(f"模板 {chosen} 未在 templates.conf 中定义")
        tpl = self.templates[chosen]

        def pick(scalar: str) -> Any:
            for source in (inst, tpl):
                value = getattr(source, scalar, None) if source else None
                if value is not None:
                    return value
            return getattr(self.defaults, scalar)

        memory = _as_int(pick("memory"), f"实例 {name} 的 memory")
        vcpus = _as_int(pick("vcpus"), f"实例 {name} 的 vcpus")
        if inst is not None and inst.capacity is not None:
            capacity = inst.capacity
        else:
            capacity = tpl.capacity

        image_dir = tpl.image_dir or self.defaults.image_dir
        image_path = (self.root / image_dir / tpl.image).resolve()

        return ResolvedInstance(
            name=name,
            template=tpl,
            instance=inst,
            image_path=image_path,
            memory=memory,
            vcpus=vcpus,
            capacity=str(capacity) if capacity is not None else None,
            network=self.defaults.network,
            firmware=self.defaults.firmware,
            graphics=pick("graphics"),
            video=pick("video"),
            shutdown_timeout=self.defaults.shutdown_timeout,
            storage_pool=self.defaults.storage_pool,
        )
