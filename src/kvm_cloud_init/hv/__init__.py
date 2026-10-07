"""libvirt 相关能力的内部子包。

注意：本模块名为 ``hv`` 而非 ``libvirt``，避免与第三方 ``libvirt``
（libvirt-python）顶层包同名导致导入歧义。
"""

from . import domains, firmware, guest, storage
from .client import DOMAIN_STATE_NAMES, connect, get_pool, pool_target_path

__all__ = [
    "DOMAIN_STATE_NAMES",
    "connect",
    "domains",
    "firmware",
    "get_pool",
    "guest",
    "pool_target_path",
    "storage",
]
