"""平台适配器注册、能力检查与解析。"""

from __future__ import annotations

from collections.abc import Iterable

from social_platform_mcp.contracts import (
    PlatformAdapter,
    PlatformCapability,
    PlatformError,
    PlatformErrorType,
    PlatformName,
)


class PlatformRegistry:
    """保存平台适配器，作为 Agent Tool 与外部连接的稳定边界。"""

    def __init__(self, adapters: Iterable[PlatformAdapter] = ()) -> None:
        self._adapters: dict[PlatformName, PlatformAdapter] = {}
        self.register_many(adapters)

    def register(self, adapter: PlatformAdapter) -> None:
        if not isinstance(adapter, PlatformAdapter):
            raise TypeError("Platform adapter does not satisfy the required protocol.")
        if adapter.platform in self._adapters:
            raise ValueError(
                f"Platform adapter is already registered: {adapter.platform.value}"
            )
        self._adapters[adapter.platform] = adapter

    def register_many(self, adapters: Iterable[PlatformAdapter]) -> None:
        pending = tuple(adapters)
        names = [adapter.platform for adapter in pending]
        if len(names) != len(set(names)) or any(
            name in self._adapters for name in names
        ):
            raise ValueError("Platform adapters must have unique platform names.")
        for adapter in pending:
            self.register(adapter)

    def names(self) -> tuple[PlatformName, ...]:
        return tuple(self._adapters)

    def resolve(
        self,
        platform: PlatformName | str,
        *,
        capability: PlatformCapability | None = None,
    ) -> PlatformAdapter:
        try:
            name = PlatformName(platform)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unsupported platform name.") from exc

        adapter = self._adapters.get(name)
        if adapter is None:
            raise PlatformError(
                platform=name,
                error_type=PlatformErrorType.UNAVAILABLE,
                message="该平台连接尚未配置。",
            )
        if capability is not None and capability not in adapter.capabilities:
            raise PlatformError(
                platform=name,
                error_type=PlatformErrorType.INVALID_REQUEST,
                message="该平台未开放请求的能力。",
            )
        return adapter
