"""三平台公共接口，不依赖觅响、模型或业务数据库。"""

from .contracts import PlatformCallBudget, PlatformCapability, PlatformError, PlatformName
from .registry import PlatformRegistry

__version__ = "0.1.0"
__all__ = ["PlatformCallBudget", "PlatformCapability", "PlatformError", "PlatformName", "PlatformRegistry"]
