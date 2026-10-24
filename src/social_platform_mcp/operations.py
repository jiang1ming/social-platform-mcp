"""平台写操作、账号绑定与只读核查的稳定契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from social_platform_mcp.contracts import (
    PlatformCapability,
    PlatformContent,
    PlatformName,
    normalize_required_text,
)


class WriteResultStatus(StrEnum):
    """外部写请求的可证明结果。"""

    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class OperationUncertainty(StrEnum):
    """写请求可能已经到达平台，但本地无法确认的原因。"""

    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


class ReconciliationStatus(StrEnum):
    """一次只读核查的结果。"""

    FOUND = "found"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"


class ReconciliationMatch(StrEnum):
    """核查命中的依据。"""

    PLATFORM_ID = "platform_id"
    EXACT_TEXT = "exact_text"


def _validate_https_url(value: str, *, name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError(f"{name} must be an HTTPS URL.")


@dataclass(frozen=True, slots=True)
class PlatformAccountIdentity:
    """内部绑定实际平台账号，公开表示不泄露稳定账号 ID。"""

    platform: PlatformName
    account_id: str = field(repr=False)
    display_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_id",
            normalize_required_text(
                self.account_id,
                name="Platform account ID",
                max_length=128,
            ),
        )
        object.__setattr__(
            self,
            "display_name",
            normalize_required_text(
                self.display_name,
                name="Platform account display name",
                max_length=128,
            ),
        )

    @property
    def safe_account_label(self) -> str:
        suffix = self.account_id[-4:] if len(self.account_id) > 4 else ""
        return f"微博账号 …{suffix}" if suffix else "微博账号（已连接）"

    def to_public_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform.value,
            "display_name": self.display_name,
            "account_label": self.safe_account_label,
        }


@dataclass(frozen=True, slots=True)
class PublicationWriteResult:
    """发布请求的确认结果；未知结果不伪造平台 ID。"""

    platform: PlatformName
    status: WriteResultStatus
    content_id: str | None = None
    content_url: str | None = None
    uncertainty_reason: OperationUncertainty | None = None

    def __post_init__(self) -> None:
        if self.status is WriteResultStatus.CONFIRMED:
            if not self.content_id or not self.content_url:
                raise ValueError("Confirmed publication requires an ID and URL.")
            _validate_https_url(self.content_url, name="Publication URL")
            if self.uncertainty_reason is not None:
                raise ValueError("Confirmed publication cannot have uncertainty.")
        elif self.content_id is not None or self.content_url is not None:
            raise ValueError("Unknown publication must not claim an ID or URL.")
        elif self.uncertainty_reason is None:
            raise ValueError("Unknown publication requires an uncertainty reason.")

    @classmethod
    def unknown(
        cls,
        *,
        platform: PlatformName,
        reason: OperationUncertainty,
    ) -> PublicationWriteResult:
        return cls(
            platform=platform,
            status=WriteResultStatus.UNKNOWN,
            uncertainty_reason=reason,
        )

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "platform": self.platform.value,
            "status": self.status.value,
            "content_id": self.content_id,
            "content_url": self.content_url,
            "uncertainty_reason": (
                self.uncertainty_reason.value if self.uncertainty_reason else None
            ),
        }


@dataclass(frozen=True, slots=True)
class PublicationLookupResult:
    """按平台 ID 或精确正文核查发布结果。"""

    platform: PlatformName
    status: ReconciliationStatus
    content: PlatformContent | None = None
    matched_by: ReconciliationMatch | None = None
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if self.candidate_count < 0:
            raise ValueError("Publication candidate count must not be negative.")
        if self.status is ReconciliationStatus.FOUND:
            if self.content is None or self.matched_by is None:
                raise ValueError("Found publication requires content and match type.")
            if self.candidate_count != 1:
                raise ValueError("Found publication must have exactly one candidate.")
        elif self.content is not None or self.matched_by is not None:
            raise ValueError("Unconfirmed lookup must not expose matched content.")


@dataclass(frozen=True, slots=True)
class PlatformCommentRecord:
    """监控所需的评论原始记录，稳定作者 ID 仅供内部去重。"""

    platform: PlatformName
    post_id: str
    comment_id: str
    text: str
    content_url: str
    author_account_id: str | None = field(default=None, repr=False)
    author_name: str | None = None
    parent_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.post_id, "Platform post ID"),
            (self.comment_id, "Platform comment ID"),
        ):
            normalize_required_text(value, name=name, max_length=128)
        _validate_https_url(self.content_url, name="Comment URL")

    def to_public_dict(self) -> dict[str, str | None]:
        return {
            "platform": self.platform.value,
            "post_id": self.post_id,
            "comment_id": self.comment_id,
            "text": self.text,
            "content_url": self.content_url,
            "author_name": self.author_name,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PlatformCommentBatch:
    """一页微博评论和下一页位置。"""

    platform: PlatformName
    post_id: str
    page: int
    count: int
    items: tuple[PlatformCommentRecord, ...]
    is_end: bool
    next_page: int | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("Comment page must be positive.")
        if not 1 <= self.count <= 200:
            raise ValueError("Comment count must be between 1 and 200.")
        if self.is_end and self.next_page is not None:
            raise ValueError("Final comment batch cannot have a next page.")
        if not self.is_end and self.next_page != self.page + 1:
            raise ValueError("Non-final comment batch must advance by one page.")


@dataclass(frozen=True, slots=True)
class ReplyWriteResult:
    """评论回复请求的确认结果。"""

    platform: PlatformName
    post_id: str
    parent_comment_id: str
    status: WriteResultStatus
    reply_id: str | None = None
    uncertainty_reason: OperationUncertainty | None = None

    def __post_init__(self) -> None:
        if self.status is WriteResultStatus.CONFIRMED:
            if not self.reply_id:
                raise ValueError("Confirmed reply requires a reply ID.")
            if self.uncertainty_reason is not None:
                raise ValueError("Confirmed reply cannot have uncertainty.")
        elif self.reply_id is not None:
            raise ValueError("Unknown reply must not claim a reply ID.")
        elif self.uncertainty_reason is None:
            raise ValueError("Unknown reply requires an uncertainty reason.")

    @classmethod
    def unknown(
        cls,
        *,
        platform: PlatformName,
        post_id: str,
        parent_comment_id: str,
        reason: OperationUncertainty,
    ) -> ReplyWriteResult:
        return cls(
            platform=platform,
            post_id=post_id,
            parent_comment_id=parent_comment_id,
            status=WriteResultStatus.UNKNOWN,
            uncertainty_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class PlatformReplyRecord:
    """只读核查得到的平台回复。"""

    platform: PlatformName
    post_id: str
    parent_comment_id: str | None
    reply_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ReplyLookupResult:
    """按回复 ID 或精确正文核查回复结果。"""

    platform: PlatformName
    status: ReconciliationStatus
    reply: PlatformReplyRecord | None = None
    matched_by: ReconciliationMatch | None = None
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if self.candidate_count < 0:
            raise ValueError("Reply candidate count must not be negative.")
        if self.status is ReconciliationStatus.FOUND:
            if self.reply is None or self.matched_by is None:
                raise ValueError("Found reply requires a record and match type.")
            if self.candidate_count != 1:
                raise ValueError("Found reply must have exactly one candidate.")
        elif self.reply is not None or self.matched_by is not None:
            raise ValueError("Unconfirmed reply lookup must not expose a record.")


@runtime_checkable
class PlatformOperationAdapter(Protocol):
    """只允许业务 Service 使用的外部操作边界。"""

    @property
    def platform(self) -> PlatformName: ...

    @property
    def capabilities(self) -> frozenset[PlatformCapability]: ...

    async def get_account_identity(self) -> PlatformAccountIdentity: ...

    async def publish_text(self, text: str) -> PublicationWriteResult: ...

    async def lookup_publication(
        self,
        *,
        content_id: str | None = None,
        expected_text: str | None = None,
    ) -> PublicationLookupResult: ...

    async def read_post_comments(
        self,
        content_id: str,
        *,
        page: int = 1,
        count: int = 50,
        since_id: str | None = None,
        max_id: str | None = None,
    ) -> PlatformCommentBatch: ...

    async def reply_comment(
        self,
        content_id: str,
        comment_id: str,
        text: str,
    ) -> ReplyWriteResult: ...

    async def lookup_reply(
        self,
        *,
        reply_id: str | None = None,
        content_id: str | None = None,
        comment_id: str | None = None,
        expected_text: str | None = None,
    ) -> ReplyLookupResult: ...
