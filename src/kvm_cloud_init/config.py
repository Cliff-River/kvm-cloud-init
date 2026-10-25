"""配置文件加载、校验与优先级合并。

文件（均位于项目根目录）：

- ``default.conf``       全局默认值，最低优先级
- ``images.conf``        系统生成的云镜像目录（内置预设渲染，缺失时自动生成）
- ``images.user.conf``   用户自定义镜像源（可选，工具永不改写）
- ``templates.conf``     OS 模板定义
- ``instances.conf``     实例定义，最高优先级

合并优先级：实例覆盖 > 模板定义 > default.conf。
镜像注册表由 ``images.conf`` 与 ``images.user.conf`` 合并而成，用户同名条目
整体覆盖系统条目。cloud-init 三份文档（meta-data / network-config /
user-data）只做整块替换，不做 YAML 深合并。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import catalog
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
#: 镜像源类型与校验算法
_VALID_IMAGE_KIND = ("url", "index")
_VALID_ORDER_BY = ("name", "mtime")
_VALID_CHECKSUM_ALGO = ("sha256", "sha512")
#: 镜像本地落盘格式；留空时按文件内容自动识别
_VALID_IMAGE_FORMAT = ("qcow2", "raw")
#: 镜像缺失时的获取策略
_VALID_IMAGE_POLICY = ("auto", "local")
_IMAGE_FIELDS = {
    "file", "kind", "arch", "urls", "pages", "match", "order_by",
    "checksum_url", "checksum_file", "checksum_match", "checksum_algo",
    "format", "insecure", "archived", "upstream_file", "upstream_version",
    "checked_at", "upstream_status",
}


def project_root() -> Path:
    """项目根目录：src/kvm_cloud_init/config.py 向上三级。"""
    return Path(__file__).resolve().parents[2]


@dataclass
class Defaults:
    storage_pool: str = "default"
    image_dir: str = "images"
    image_policy: str = "auto"
    memory: int = 6144
    vcpus: int = 6
    network: str = "default"
    firmware: str = "auto"
    graphics: str = "vnc"
    video: str = "auto"
    level: str = "normal"
    shutdown_timeout: int = 120


@dataclass(frozen=True)
class ImageSource:
    """镜像目录中的一个条目（系统预设或用户自定义）。"""

    id: str
    file: str
    kind: str
    urls: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    match: str | None = None
    order_by: str = "name"
    checksum_url: str | None = None
    checksum_file: str | None = None
    checksum_match: str | None = None
    checksum_algo: str = "sha256"
    format: str | None = None
    insecure: bool = False
    archived: bool = False
    arch: str = "x86_64"
    upstream_file: str | None = None
    upstream_version: str | None = None
    checked_at: str | None = None
    upstream_status: str | None = None
    origin: str = "system"


@dataclass
class Template:
    """templates.conf 中的一个 OS 模板。"""

    name: str
    image: str
    path: str | None = None
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
    image_id: str
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


def _as_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be a boolean, got: {value!r}")
    return value


def _as_urls(value: Any, context: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list of URLs")
    urls = tuple(str(item) for item in value)
    for url in urls:
        if not url.startswith(("http://", "https://", "file://")):
            raise ConfigError(f"{context} contains an unsupported URL: {url}")
    return urls


def _parse_image_source(image_id: str, raw: Any, origin: str) -> ImageSource:
    if not isinstance(raw, dict):
        raise ConfigError(f"Image {image_id} definition must be a mapping")
    unknown = set(raw) - _IMAGE_FIELDS
    if unknown:
        raise ConfigError(
            f"Unknown fields in image {image_id}: {', '.join(sorted(unknown))}"
        )
    for required in ("file", "kind"):
        if required not in raw or not raw[required]:
            raise ConfigError(f"Image {image_id} is missing required field {required}")

    filename = str(raw["file"])
    if filename != Path(filename).name:
        raise ConfigError(
            f"Image {image_id} field file must be a plain file name, got: {filename}"
        )
    kind = _choice(raw["kind"], _VALID_IMAGE_KIND, f"image {image_id} field kind")
    order_by = _choice(
        raw.get("order_by", "name"),
        _VALID_ORDER_BY,
        f"image {image_id} field order_by",
    )
    checksum_algo = _choice(
        raw.get("checksum_algo", "sha256"),
        _VALID_CHECKSUM_ALGO,
        f"image {image_id} field checksum_algo",
    )

    urls: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    match: str | None = None
    checksum_url: str | None = None
    checksum_file: str | None = None
    checksum_match: str | None = None

    if kind == "url":
        urls = _as_urls(raw.get("urls"), f"image {image_id} field urls")
        checksum_url = str(raw["checksum_url"]) if raw.get("checksum_url") else None
        if raw.get("pages") or raw.get("match") or raw.get("checksum_match"):
            raise ConfigError(
                f"Image {image_id} uses kind url; pages/match/checksum_match "
                "are only valid for kind index"
            )
    else:
        pages = _as_urls(raw.get("pages"), f"image {image_id} field pages")
        if not raw.get("match"):
            raise ConfigError(f"Image {image_id} is missing required field match")
        match = str(raw["match"])
        try:
            re.compile(match)
        except re.error as exc:
            raise ConfigError(
                f"Image {image_id} field match is not a valid regular expression: {exc}"
            ) from exc
        checksum_file = (
            str(raw["checksum_file"]) if raw.get("checksum_file") else None
        )
        checksum_match = (
            str(raw["checksum_match"]) if raw.get("checksum_match") else None
        )
        if checksum_match:
            try:
                re.compile(checksum_match)
            except re.error as exc:
                raise ConfigError(
                    f"Image {image_id} field checksum_match is not a valid "
                    f"regular expression: {exc}"
                ) from exc
        if raw.get("urls") or raw.get("checksum_url"):
            raise ConfigError(
                f"Image {image_id} uses kind index; urls/checksum_url "
                "are only valid for kind url"
            )

    if not (checksum_url or checksum_file or checksum_match) and not raw.get(
        "insecure"
    ):
        raise ConfigError(
            f"Image {image_id} has no checksum source; set checksum_url/"
            "checksum_file/checksum_match or explicitly insecure: true"
        )

    return ImageSource(
        id=image_id,
        file=filename,
        kind=kind,
        urls=urls,
        pages=pages,
        match=match,
        order_by=order_by,
        checksum_url=checksum_url,
        checksum_file=checksum_file,
        checksum_match=checksum_match,
        checksum_algo=checksum_algo,
        format=(
            _choice(raw["format"], _VALID_IMAGE_FORMAT, f"image {image_id} field format")
            if raw.get("format")
            else None
        ),
        insecure=_as_bool(raw.get("insecure", False), f"image {image_id} field insecure"),
        archived=_as_bool(raw.get("archived", False), f"image {image_id} field archived"),
        arch=str(raw.get("arch", "x86_64")),
        upstream_file=str(raw["upstream_file"]) if raw.get("upstream_file") else None,
        upstream_version=(
            str(raw["upstream_version"]) if raw.get("upstream_version") else None
        ),
        checked_at=str(raw["checked_at"]) if raw.get("checked_at") else None,
        upstream_status=(
            str(raw["upstream_status"]) if raw.get("upstream_status") else None
        ),
        origin=origin,
    )


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
    if "image_policy" in raw:
        defaults.image_policy = _choice(
            raw["image_policy"], _VALID_IMAGE_POLICY, "default.conf field image_policy"
        )
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
        "path", "image", "os_variant", "memory", "vcpus",
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
    """加载并持有全部配置，提供镜像注册表查询与实例解析。"""

    def __init__(
        self,
        root: Path,
        defaults: Defaults,
        templates: dict[str, Template],
        instances: dict[str, InstanceSpec],
        images: dict[str, ImageSource],
    ) -> None:
        self.root = root
        self.defaults = defaults
        self.templates = templates
        self.instances = instances
        self.images = images

    @property
    def image_dir(self) -> Path:
        return self.root / self.defaults.image_dir

    def image_path(self, image_id: str) -> Path:
        """镜像 ID 对应的本地固定文件路径。"""
        return (self.image_dir / self.images[image_id].file).resolve()

    def template_image_ids(self) -> list[str]:
        """模板引用到的镜像 ID（按模板顺序去重）。"""
        seen: dict[str, None] = {}
        for tpl in self.templates.values():
            seen.setdefault(tpl.image, None)
        return list(seen)

    @classmethod
    def load(cls, root: Path | str | None = None) -> ConfigStore:
        base = Path(root) if root else project_root()
        defaults = _parse_defaults(_load_yaml(base / "default.conf"))

        # 顶层即模板/实例表（文件名已表明语义，不再使用包装键）。
        # templates / instances / images 为保留名，用于识别旧格式并给出迁移提示。
        templates = {
            name: _parse_template(name, body)
            for name, body in _load_table(base / "templates.conf", "templates").items()
        }
        instances = {
            name: _parse_instance(name, body)
            for name, body in _load_table(base / "instances.conf", "instances").items()
        }
        images = _load_images(base)

        # 交叉校验：实例引用的模板必须存在，模板引用的镜像 ID 必须存在
        for inst in instances.values():
            if inst.template not in templates:
                raise ConfigError(
                    f"Instance {inst.name} references template {inst.template}, "
                    "which is not defined in templates.conf"
                )
        for tpl in templates.values():
            if tpl.image not in images:
                raise ConfigError(
                    f"Template {tpl.name} references image {tpl.image}, which is "
                    f"not defined in {catalog.CATALOG_FILENAME} or "
                    f"{catalog.USER_CATALOG_FILENAME}"
                )
        _reject_duplicate_files(images)
        return cls(base, defaults, templates, instances, images)

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

        return ResolvedInstance(
            name=name,
            template=tpl,
            instance=inst,
            image_id=tpl.image,
            image_path=self.image_path(tpl.image),
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


def _load_images(base: Path) -> dict[str, ImageSource]:
    """加载并合并系统生成目录与用户自定义目录（用户同名条目整体覆盖）。"""
    system_path = base / catalog.CATALOG_FILENAME
    if not system_path.exists():
        _write_catalog(system_path, catalog.render_catalog())

    images: dict[str, ImageSource] = {}
    for image_id, body in _load_table(system_path, "images").items():
        images[image_id] = _parse_image_source(image_id, body, "system")

    user_path = base / catalog.USER_CATALOG_FILENAME
    if user_path.exists():
        for image_id, body in _load_table(user_path, "images").items():
            origin = "user(override)" if image_id in images else "user"
            images[image_id] = _parse_image_source(image_id, body, origin)
    return images


def _write_catalog(path: Path, text: str) -> None:
    """原子写入生成的 images.conf（先临时文件再 os.replace）。"""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _reject_duplicate_files(images: dict[str, ImageSource]) -> None:
    owners: dict[str, str] = {}
    for image_id, source in images.items():
        previous = owners.get(source.file)
        if previous is not None:
            raise ConfigError(
                f"Images {previous} and {image_id} both map to local file "
                f"{source.file}; local file names must be unique"
            )
        owners[source.file] = image_id

