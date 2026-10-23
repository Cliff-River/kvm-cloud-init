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
#: 删除保护级别：normal 可直接删 / production 需确认 / protected 拒绝删除
_VALID_LEVEL = ("normal", "production", "protected")
#: 模板/实例可覆盖的字符串型枚举选项（graphics/video/level）
_CHOICE_FIELDS = ("graphics", "video", "level")


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
    level: str = "normal"
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
    level: str | None = None
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
    level: str | None = None
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
    level: str
    shutdown_timeout: int
    storage_pool: str


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file {path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level structure of config file {path} must be a mapping")
    return data


def _load_table(path: Path, legacy_wrapper: str) -> dict[str, Any]:
    """加载 templates/instances 配置：根映射即为定义表。

    检测旧版包装键（templates:/instances:）并拒绝，提示去掉一层缩进。
    """
    data = _load_yaml(path)
    if legacy_wrapper in data and isinstance(data[legacy_wrapper], dict):
        raise ConfigError(
            f"{path.name} still uses the deprecated top-level key {legacy_wrapper!r}; "
            "move its entries to the top level (remove the key and one indentation level)"
        )
    return data


def _as_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{context} must be an integer, got: {value!r}")
    if value <= 0:
        raise ConfigError(f"{context} must be a positive integer, got: {value}")
    return value


def _choice(value: Any, options: tuple[str, ...], context: str) -> str:
    text = str(value).lower()
    if text not in options:
        raise ConfigError(
            f"{context} must be one of {'/'.join(options)}, got: {value!r}"
        )
    return text


def _parse_defaults(raw: dict[str, Any]) -> Defaults:
    allowed = set(Defaults.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown fields in default.conf: {', '.join(sorted(unknown))}")
    defaults = Defaults()
    if "storage_pool" in raw:
        defaults.storage_pool = str(raw["storage_pool"])
    if "image_dir" in raw:
        defaults.image_dir = str(raw["image_dir"])
    if "memory" in raw:
        defaults.memory = _as_int(raw["memory"], "default.conf field memory")
    if "vcpus" in raw:
        defaults.vcpus = _as_int(raw["vcpus"], "default.conf field vcpus")
    if "network" in raw:
        defaults.network = str(raw["network"])
    if "firmware" in raw:
        defaults.firmware = _choice(raw["firmware"], _VALID_FIRMWARE, "default.conf field firmware")
    if "graphics" in raw:
        defaults.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, "default.conf field graphics")
    if "video" in raw:
        defaults.video = _choice(raw["video"], _VALID_VIDEO, "default.conf field video")
    if "level" in raw:
        defaults.level = _choice(raw["level"], _VALID_LEVEL, "default.conf field level")
    if "shutdown_timeout" in raw:
        defaults.shutdown_timeout = _as_int(
            raw["shutdown_timeout"], "default.conf field shutdown_timeout"
        )
    return defaults


def _extract_docs(raw: dict[str, Any], context: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    for name in DOC_NAMES:
        if name in raw:
            value = raw[name]
            if value is not None and not isinstance(value, str):
                raise ConfigError(
                    f"{context} field {name} must be a text block (use the | scalar)"
                )
            if value:
                docs[name] = value
    return docs


def _parse_template(name: str, raw: Any) -> Template:
    if not isinstance(raw, dict):
        raise ConfigError(f"Template {name} definition must be a mapping")
    if "image" not in raw or not raw["image"]:
        raise ConfigError(f"Template {name} is missing required field image")
    tpl = Template(name=name, image=str(raw["image"]))
    if "path" in raw and raw["path"] is not None:
        tpl.path = str(raw["path"])
    if "image_dir" in raw and raw["image_dir"] is not None:
        tpl.image_dir = str(raw["image_dir"])
    if "os_variant" in raw and raw["os_variant"] is not None:
        tpl.os_variant = str(raw["os_variant"])
    if "memory" in raw and raw["memory"] is not None:
        tpl.memory = _as_int(raw["memory"], f"template {name} field memory")
    if "vcpus" in raw and raw["vcpus"] is not None:
        tpl.vcpus = _as_int(raw["vcpus"], f"template {name} field vcpus")
    if "capacity" in raw and raw["capacity"] is not None:
        tpl.capacity = str(raw["capacity"])
    if raw.get("graphics") is not None:
        tpl.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, f"template {name} field graphics")
    if raw.get("video") is not None:
        tpl.video = _choice(raw["video"], _VALID_VIDEO, f"template {name} field video")
    if raw.get("level") is not None:
        tpl.level = _choice(raw["level"], _VALID_LEVEL, f"template {name} field level")
    tpl.docs = _extract_docs(raw, f"template {name}")
    allowed = {
        "path", "image_dir", "image", "os_variant", "memory", "vcpus",
        "capacity", *_CHOICE_FIELDS, *DOC_NAMES,
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown fields in template {name}: {', '.join(sorted(unknown))}")
    if not tpl.path and not tpl.docs:
        raise ConfigError(f"Template {name} must set path or inline cloud-init fields")
    return tpl


def _parse_instance(name: str, raw: Any) -> InstanceSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"Instance {name} definition must be a mapping")
    if "template" not in raw or not raw["template"]:
        raise ConfigError(f"Instance {name} is missing required field template")
    inst = InstanceSpec(name=name, template=str(raw["template"]))
    for scalar in ("memory", "vcpus"):
        if scalar in raw and raw[scalar] is not None:
            setattr(inst, scalar, _as_int(raw[scalar], f"instance {name} field {scalar}"))
    if "capacity" in raw and raw["capacity"] is not None:
        inst.capacity = str(raw["capacity"])
    if raw.get("graphics") is not None:
        inst.graphics = _choice(raw["graphics"], _VALID_GRAPHICS, f"instance {name} field graphics")
    if raw.get("video") is not None:
        inst.video = _choice(raw["video"], _VALID_VIDEO, f"instance {name} field video")
    if raw.get("level") is not None:
        inst.level = _choice(raw["level"], _VALID_LEVEL, f"instance {name} field level")
    inst.docs = _extract_docs(raw, f"instance {name}")
    allowed = {"template", *_SCALAR_FIELDS, *_CHOICE_FIELDS, *DOC_NAMES}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown fields in instance {name}: {', '.join(sorted(unknown))}")
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
                    f"Instance {inst.name} references template {inst.template}, "
                    "which is not defined in templates.conf"
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
                f"Instance {name} is not defined in instances.conf; "
                "use --template to create an unregistered instance"
            )
        chosen = template_name or (inst.template if inst else None)
        if chosen not in self.templates:
            raise TemplateNotFound(f"Template {chosen} is not defined in templates.conf")
        tpl = self.templates[chosen]

        def pick(scalar: str) -> Any:
            for source in (inst, tpl):
                value = getattr(source, scalar, None) if source else None
                if value is not None:
                    return value
            return getattr(self.defaults, scalar)

        memory = _as_int(pick("memory"), f"instance {name} field memory")
        vcpus = _as_int(pick("vcpus"), f"instance {name} field vcpus")
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
            level=pick("level"),
            shutdown_timeout=self.defaults.shutdown_timeout,
            storage_pool=self.defaults.storage_pool,
        )
