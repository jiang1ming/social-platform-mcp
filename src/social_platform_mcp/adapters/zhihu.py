"""官方知乎搜索与第三方社区读取的组合适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from social_platform_mcp.contracts import (
    PlatformAccountStatus,
    PlatformCallBudget,
    PlatformCapability,
    PlatformCommentPage,
    PlatformContent,
    PlatformError,
    PlatformErrorType,
    PlatformName,
    PlatformSearchResult,
    normalize_required_text,
)
from social_platform_mcp.adapters.zhihu_community import ZhihuCommunityCliDriver


_MAX_COMMAND_OUTPUT_BYTES = 4_000_000
_ZHIHU_HOSTS = frozenset({"www.zhihu.com", "zhuanlan.zhihu.com"})


@dataclass(frozen=True, slots=True)
class ZhihuCommandResult:
    """可替换的知乎 CLI 运行结果。"""

    returncode: int
    stdout: str
    stderr: str = ""


ZhihuCommandRunner = Callable[
    [Sequence[str], float],
    Awaitable[ZhihuCommandResult],
]


async def _run_command(
    arguments: Sequence[str],
    timeout: float,
) -> ZhihuCommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=PlatformErrorType.UNAVAILABLE,
            message="知乎 CLI 不可用。",
        ) from exc

    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=PlatformErrorType.TIMEOUT,
            message="知乎 CLI 读取超时。",
            retryable=True,
        ) from exc
    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=PlatformErrorType.INVALID_RESPONSE,
            message="知乎 CLI 返回内容超过安全上限。",
        )
    return ZhihuCommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class ZhihuAdapter:
    """官方 CLI 搜索文章，社区 CLI 根据 URL 读取详情和评论。"""

    platform = PlatformName.ZHIHU
    capabilities = frozenset(
        {
            PlatformCapability.ACCOUNT_STATUS,
            PlatformCapability.SEARCH,
            PlatformCapability.DETAIL,
            PlatformCapability.COMMENTS,
        }
    )

    def __init__(
        self,
        *,
        cli_path: str = "zhihu-cli",
        budget: PlatformCallBudget | None = None,
        runner: ZhihuCommandRunner | None = None,
        community: ZhihuCommunityCliDriver | None = None,
    ) -> None:
        self._cli_path = normalize_required_text(
            cli_path,
            name="Zhihu CLI path",
            max_length=1_024,
        )
        self._budget = budget or PlatformCallBudget()
        self._runner = runner or _run_command
        self._community = community

    async def _invoke(self, *arguments: str) -> Any:
        result = await self._runner(
            (self._cli_path, *arguments),
            self._budget.timeout_seconds,
        )
        if result.returncode != 0:
            self._raise_cli_error(result.stderr or result.stdout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="知乎 CLI 返回了无效 JSON。",
            ) from exc

    def _raise_cli_error(self, raw_message: str) -> None:
        message = raw_message.casefold()
        if any(word in message for word in ("auth_required", "auth_invalid", "鉴权", "secret")):
            error_type = PlatformErrorType.NOT_AUTHENTICATED
            safe_message = "知乎 Access Secret 未配置或已失效。"
            retryable = False
        elif any(word in message for word in ("20001", "permission", "授权拒绝")):
            error_type = PlatformErrorType.PERMISSION_DENIED
            safe_message = "知乎开放平台未授权该读取能力。"
            retryable = False
        elif any(word in message for word in ("30001", "30002", "quota", "rate")):
            error_type = PlatformErrorType.RATE_LIMITED
            safe_message = "知乎接口频率受限或当日额度不足。"
            retryable = False
        elif any(word in message for word in ("timeout", "network_error", "超时")):
            error_type = PlatformErrorType.TIMEOUT
            safe_message = "知乎 CLI 读取超时。"
            retryable = True
        else:
            error_type = PlatformErrorType.UNAVAILABLE
            safe_message = "知乎 CLI 当前不可用。"
            retryable = True
        raise PlatformError(
            platform=self.platform,
            error_type=error_type,
            message=safe_message,
            retryable=retryable,
        )

    async def check_account(self) -> PlatformAccountStatus:
        payload = await self._invoke("auth", "status")
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="知乎账号状态格式无效。",
            )
        configured = bool(payload.get("ok") and payload.get("source"))
        return PlatformAccountStatus(self.platform, configured, configured)

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> PlatformSearchResult:
        normalized_query = normalize_required_text(
            query,
            name="Zhihu search query",
            max_length=100,
        )
        requested = self._validated_limit(limit, maximum=10)
        fetch_count = min(max(requested * 3, requested), 10)
        payload = await self._invoke(
            "search",
            "zhihu",
            "--query",
            normalized_query,
            "--count",
            str(fetch_count),
        )
        records = self._extract_records(payload)
        article_items = tuple(
            content
            for record in records
            if (content := self._normalize_content(record)) is not None
        )
        items = article_items[:requested]
        return PlatformSearchResult(
            self.platform,
            normalized_query,
            items,
            len(article_items) > requested or len(records) >= fetch_count,
        )

    async def get_detail(self, content_id: str) -> PlatformContent:
        return await self._require_community().get_article(content_id)

    async def check_community_account(self) -> PlatformAccountStatus:
        """社区 Cookie 与官方搜索凭据分别检查，避免误报整体可用。"""
        return await self._require_community().check_account()

    async def read_comments(
        self,
        content_url: str,
        *,
        limit: int = 20,
    ) -> PlatformCommentPage:
        return await self._require_community().read_comments(
            content_url,
            limit=limit,
        )

    def _require_community(self) -> ZhihuCommunityCliDriver:
        if self._community is None:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.UNAVAILABLE,
                message="知乎社区读取尚未配置。",
            )
        return self._community

    def _validated_limit(self, limit: int | None, *, maximum: int) -> int:
        requested = self._budget.max_results if limit is None else limit
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise ValueError("Zhihu result limit must be a positive integer.")
        return min(requested, self._budget.max_results, maximum)

    @staticmethod
    def _extract_records(payload: object) -> list[dict[str, Any]]:
        current = payload
        for _ in range(4):
            if isinstance(current, list):
                return [item for item in current if isinstance(item, dict)]
            if not isinstance(current, dict):
                return []
            for key in ("Items", "items", "Contents", "contents", "List", "list"):
                value = current.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            current = current.get("Data", current.get("data"))
        return []

    def _normalize_content(
        self,
        record: dict[str, Any],
    ) -> PlatformContent | None:
        url = self._first_string(record, "Url", "URL", "url")
        if url is None:
            return None
        try:
            url = self._validate_content_url(url)
        except ValueError:
            return None
        if not self._is_column_article_url(url):
            return None
        content_id = (
            self._first_string(
                record,
                "ContentToken",
                "ContentID",
                "Token",
                "ID",
                "Id",
                "id",
            )
            or url
        )
        title = self._first_string(record, "Title", "title") or "知乎内容"
        text = self._first_string(
            record,
            "ContentText",
            "Summary",
            "Excerpt",
            "content_text",
            "summary",
        ) or ""
        author = self._first_string(record, "AuthorName", "author_name")
        return PlatformContent(
            self.platform,
            content_id[:256],
            title[:200],
            text[:4_000],
            url,
            author_name=author,
            metrics=self._search_metrics(record),
        )

    @staticmethod
    def _search_metrics(record: dict[str, Any]) -> dict[str, int | str]:
        metrics: dict[str, int | str] = {}
        for source, target in (
            ("VoteUpCount", "voteup_count"),
            ("CommentCount", "comment_count"),
            ("FavoriteCount", "favorite_count"),
            ("AnswerCount", "answer_count"),
            ("FollowerCount", "follower_count"),
            ("EditTime", "updated_at"),
        ):
            value = record.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                metrics[target] = value
        return metrics

    @staticmethod
    def _first_string(record: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
        return None

    @staticmethod
    def _validate_content_url(value: str) -> str:
        normalized = normalize_required_text(
            value,
            name="Zhihu content URL",
            max_length=1_024,
        )
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or parsed.hostname not in _ZHIHU_HOSTS:
            raise ValueError("Zhihu content URL must use an approved HTTPS host.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Zhihu content URL must not include credentials.")
        return normalized

    @staticmethod
    def _is_column_article_url(value: str) -> bool:
        parsed = urlsplit(value)
        segments = [segment for segment in parsed.path.split("/") if segment]
        return (
            parsed.hostname == "zhuanlan.zhihu.com"
            and len(segments) == 2
            and segments[0] == "p"
            and segments[1].isdigit()
        )
