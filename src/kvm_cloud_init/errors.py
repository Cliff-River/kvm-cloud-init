"""自定义异常：向 CLI 层提供可直接展示给用户的错误信息（英文）。"""


class KciError(Exception):
    """所有业务异常的基类，message 可直接打印给用户。"""


class ConfigError(KciError):
    """配置文件缺失、格式或字段不合法。"""


class InstanceNotFound(ConfigError):
    """instances.conf 中找不到指定实例。"""


class TemplateNotFound(ConfigError):
    """templates.conf 中找不到指定模板。"""


class ImageNotFound(KciError):
    """本地源 qcow2 镜像文件不存在。"""


class ImageFetchError(KciError):
    """镜像目录解析、下载或校验和验证失败。"""


class LibvirtConnectionError(KciError):
    """无法连接 qemu:///system。"""


class ShutdownTimeout(KciError):
    """虚拟机在限定时间内未能优雅关闭。"""


class ProvisionError(KciError):
    """创建 / 销毁流程中的其他运行时错误。"""


class NetworkError(KciError):
    """无法确定虚拟机的实际 IP 地址（不信 network-config 声明值）。"""


class SshError(KciError):
    """SSH 密钥缺失 / 连接失败等与远程登录相关的错误。"""


class ProtectionDenied(ProvisionError):
    """删除保护级别阻止了销毁 / 重建（protected，或 production 未确认）。"""


class CloudInitError(ProvisionError):
    """cloud-init 查询、等待或日志读取失败。"""
