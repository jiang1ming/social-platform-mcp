"""平台适配层共享的数据契约与安全错误。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit


class PlatformName(StrEnum):
    """V1 已接入的平台标识。"""

    WEIBO = "weibo"
    XIAOHONGSHU = "xiaohongshu"
    ZHIHU = "zhihu"


class PlatformCapability(StrEnum):
    """平台适配器可明确声明的受控能力。"""

    ACCOUNT_STATUS = "account_status"
    SEARCH = "search"
    DETAIL = "detail"
    COMMENTS = "comments"
    PUBLISH = "publish"
    REPLY = "reply"


class PlatformErrorType(StrEnum):
    """供业务层稳定处理的平台失败类别。"""

    INVALID_REQUEST = "invalid_request"
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class PlatformError(RuntimeError):
    """不携带底层凭据或原始异常文本的平台错误。"""

    def __init__(
        self,
        *,
        platform: PlatformName,
        error_type: PlatformErrorType,
        message: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.platform = platform
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PlatformCallBudget:
    """限制一次平台读取的等待时间和返回数量。"""

    max_results: int = 10
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= 20
        ):
            raise ValueError("Platform max results must be between 1 and 20.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= 120
        ):
            raise ValueError(
                "Platform timeout must be greater than 0 and no greater than 120."
            )
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True, slots=True)
class PlatformAccountStatus:
    """不暴露账号 ID 或令牌的连接状态。"""

    platform: PlatformName
    authenticated: bool
    ready: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "authenticated": self.authenticated,
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class PlatformContent:
    """跨平台统一的公开内容摘要。"""

    platform: PlatformName
    content_id: str
    title: str
    text: str
    source_url: str
    author_name: str | None = None
    metrics: dict[str, int | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_id.strip():
            raise ValueError("Platform content ID must not be empty.")
        if not self.source_url.strip():
            raise ValueError("Platform source URL must not be empty.")
        parsed = urlsplit(self.source_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ValueError("Platform source URL must be an HTTPS URL.")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "content_id": self.content_id,
            "title": self.title,
            "text": self.text,
            "source_url": self.source_url,
            "author_name": self.author_name,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class PlatformSearchResult:
    """一次有上限的平台搜索结果。"""

    platform: PlatformName
    query: str
    items: tuple[PlatformContent, ...]
    truncated: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "query": self.query,
            "items": [item.to_public_dict() for item in self.items],
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class PlatformComment:
    """跨平台统一的评论摘要，不携带作者内部标识。"""

    platform: PlatformName
    comment_id: str
    content_url: str
    text: str
    author_name: str | None = None
    parent_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        if not self.comment_id.strip():
            raise ValueError("Platform comment ID must not be empty.")
        parsed = urlsplit(self.content_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ValueError("Platform comment content URL must be HTTPS.")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "comment_id": self.comment_id,
            "content_url": self.content_url,
            "text": self.text,
            "author_name": self.author_name,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PlatformCommentPage:
    """一页评论与服务端返回的下一分页位置。"""

    platform: PlatformName
    content_url: str
    items: tuple[PlatformComment, ...]
    is_end: bool
    next_offset: int | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.content_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ValueError("Platform comment page URL must be HTTPS.")
        if self.next_offset is not None and self.next_offset < 0:
            raise ValueError("Next comment offset must not be negative.")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform.value,
            "content_url": self.content_url,
            "items": [item.to_public_dict() for item in self.items],
            "is_end": self.is_end,
            "next_offset": self.next_offset,
        }


def normalize_required_text(value: object, *, name: str, max_length: int) -> str:
    """校验平台查询参数，避免空值和无界输入。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{name} must not exceed {max_length} characters.")
    return normalized


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台实现必须满足的最小只读接口。"""

    @property
    def platform(self) -> PlatformName: ...

    @property
    def capabilities(self) -> frozenset[PlatformCapability]: ...

    async def check_account(self) -> PlatformAccountStatus: ...

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> PlatformSearchResult: ...

    async def get_detail(self, content_id: str) -> PlatformContent: ...
