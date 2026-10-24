"""小红书 Streamable HTTP MCP 的白名单只读适配器。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from social_platform_mcp.contracts import (
    PlatformAccountStatus,
    PlatformCallBudget,
    PlatformCapability,
    PlatformContent,
    PlatformError,
    PlatformErrorType,
    PlatformName,
    PlatformSearchResult,
    normalize_required_text,
)


_ALLOWED_TOOLS = frozenset(
    {"check_login_status", "search_feeds", "get_feed_detail"}
)
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MCPToolResponse:
    """从 MCP SDK 结果中抽取的最小安全响应。"""

    texts: tuple[str, ...]
    is_error: bool = False


class MCPToolCaller(Protocol):
    async def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> MCPToolResponse: ...


def create_streamable_http_caller(endpoint_url: str) -> MCPToolCaller:
    """创建每次调用都完整关闭会话的 MCP 客户端。"""

    async def call(
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> MCPToolResponse:
        if tool_name not in _ALLOWED_TOOLS:
            raise PlatformError(
                platform=PlatformName.XIAOHONGSHU,
                error_type=PlatformErrorType.INVALID_REQUEST,
                message="小红书工具未列入只读白名单。",
            )
        total_started = time.perf_counter()
        stage = "connect"
        stage_started = total_started
        timings: dict[str, float | None] = {
            "connect": None,
            "initialize": None,
            "tool": None,
            "close": None,
        }
        outcome = "unavailable"
        result: Any | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                async with streamable_http_client(endpoint_url) as streams:
                    timings["connect"] = time.perf_counter() - stage_started
                    read_stream, write_stream = streams
                    stage = "initialize"
                    stage_started = time.perf_counter()
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timeout_seconds,
                    ) as session:
                        await session.initialize()
                        timings["initialize"] = time.perf_counter() - stage_started
                        stage = "tool"
                        stage_started = time.perf_counter()
                        result = await session.call_tool(tool_name, arguments)
                        timings["tool"] = time.perf_counter() - stage_started
                        stage = "close"
                        stage_started = time.perf_counter()
                timings["close"] = time.perf_counter() - stage_started
                stage = "completed"
            outcome = "tool_error" if bool(result.is_error) else "succeeded"
        except TimeoutError as exc:
            outcome = "timeout"
            raise PlatformError(
                platform=PlatformName.XIAOHONGSHU,
                error_type=PlatformErrorType.TIMEOUT,
                message="小红书 MCP 读取超时。",
                retryable=True,
            ) from exc
        except PlatformError:
            outcome = "platform_error"
            raise
        except Exception as exc:
            outcome = "unavailable"
            raise PlatformError(
                platform=PlatformName.XIAOHONGSHU,
                error_type=PlatformErrorType.UNAVAILABLE,
                message="小红书 MCP 当前不可用。",
                retryable=True,
            ) from exc
        finally:
            if stage in timings and timings[stage] is None:
                timings[stage] = time.perf_counter() - stage_started
            total_seconds = time.perf_counter() - total_started
            log_level = (
                logging.INFO
                if outcome in {"succeeded", "tool_error"}
                else logging.WARNING
            )
            _LOGGER.log(
                log_level,
                (
                    "xiaohongshu_mcp_call tool=%s outcome=%s stage=%s "
                    "connect_ms=%.1f initialize_ms=%.1f tool_ms=%.1f "
                    "close_ms=%.1f total_ms=%.1f"
                ),
                tool_name,
                outcome,
                stage,
                *((timings[name] or 0.0) * 1_000 for name in timings),
                total_seconds * 1_000,
            )

        assert result is not None
        texts = tuple(
            item.text
            for item in result.content
            if getattr(item, "type", None) == "text"
            and isinstance(getattr(item, "text", None), str)
        )
        return MCPToolResponse(texts=texts, is_error=bool(result.is_error))

    return call


class XiaohongshuAdapter:
    """只暴露登录检查、搜索和详情，并在内存中隐藏访问令牌。"""

    platform = PlatformName.XIAOHONGSHU
    capabilities = frozenset(
        {
            PlatformCapability.ACCOUNT_STATUS,
            PlatformCapability.SEARCH,
            PlatformCapability.DETAIL,
        }
    )

    def __init__(
        self,
        *,
        endpoint_url: str = "http://127.0.0.1:18060/mcp",
        budget: PlatformCallBudget | None = None,
        caller: MCPToolCaller | None = None,
    ) -> None:
        self._endpoint_url = normalize_required_text(
            endpoint_url,
            name="Xiaohongshu MCP URL",
            max_length=2_048,
        )
        parsed = urlsplit(self._endpoint_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or (
                parsed.scheme == "http"
                and parsed.hostname.casefold() not in _LOCAL_HTTP_HOSTS
            )
        ):
            raise ValueError(
                "Xiaohongshu MCP URL must use HTTPS or local HTTP without credentials."
            )
        self._budget = budget or PlatformCallBudget()
        self._caller = caller or create_streamable_http_caller(self._endpoint_url)
        self._access_tokens: dict[str, str] = {}
        self._token_times: dict[str, float] = {}

    async def _call(self, name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
        response = await self._caller(
            name,
            arguments,
            self._budget.timeout_seconds,
        )
        if response.is_error:
            self._raise_mcp_error("\n".join(response.texts))
        return response.texts

    def _raise_mcp_error(self, raw_message: str) -> None:
        message = raw_message.casefold()
        if any(word in message for word in ("未登录", "not logged", "login")):
            error_type = PlatformErrorType.NOT_AUTHENTICATED
            safe_message = "小红书账号未登录或登录已失效。"
            retryable = False
        elif any(word in message for word in ("额度", "quota", "rate limit", "429")):
            error_type = PlatformErrorType.RATE_LIMITED
            safe_message = "小红书读取额度不足或请求频率受限。"
            retryable = True
        elif any(word in message for word in ("timeout", "超时")):
            error_type = PlatformErrorType.TIMEOUT
            safe_message = "小红书 MCP 读取超时。"
            retryable = True
        else:
            error_type = PlatformErrorType.UNAVAILABLE
            safe_message = "小红书 MCP 工具调用失败。"
            retryable = True
        raise PlatformError(
            platform=self.platform,
            error_type=error_type,
            message=safe_message,
            retryable=retryable,
        )

    async def check_account(self) -> PlatformAccountStatus:
        texts = await self._call("check_login_status", {})
        rendered = "\n".join(texts)
        if "未登录" not in rendered and "已登录" not in rendered:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="小红书登录状态格式无效。",
            )
        authenticated = "未登录" not in rendered and "已登录" in rendered
        return PlatformAccountStatus(
            platform=self.platform,
            authenticated=authenticated,
            ready=authenticated,
        )

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> PlatformSearchResult:
        normalized_query = normalize_required_text(
            query,
            name="Xiaohongshu search query",
            max_length=100,
        )
        requested = self._validated_limit(limit)
        texts = await self._call("search_feeds", {"keyword": normalized_query})
        payload = self._parse_json_text(texts, "小红书搜索结果格式无效。")
        feeds = payload.get("feeds") if isinstance(payload, dict) else None
        if not isinstance(feeds, list):
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="小红书搜索结果格式无效。",
            )

        normalized_items: list[PlatformContent] = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            content_id = str(feed.get("id") or "").strip()
            access_token = feed.get("xsecToken")
            if not content_id or not isinstance(access_token, str) or not access_token:
                continue
            self._access_tokens[content_id] = access_token
            self._token_times[content_id] = time.monotonic()
            while len(self._access_tokens) > 1_000:
                oldest = next(iter(self._access_tokens))
                self._access_tokens.pop(oldest, None)
                self._token_times.pop(oldest, None)
            normalized_items.append(self._normalize_search_item(feed))
            if len(normalized_items) >= requested:
                break

        return PlatformSearchResult(
            platform=self.platform,
            query=normalized_query,
            items=tuple(normalized_items),
            truncated=len(feeds) > len(normalized_items),
        )

    async def get_detail(self, content_id: str) -> PlatformContent:
        normalized_id = normalize_required_text(
            content_id,
            name="Xiaohongshu content ID",
            max_length=128,
        )
        access_token = self._access_tokens.get(normalized_id)
        if time.monotonic() - self._token_times.get(normalized_id, 0) > 1_800:
            self._access_tokens.pop(normalized_id, None)
            self._token_times.pop(normalized_id, None)
            access_token = None
        if access_token is None:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_REQUEST,
                message="请先搜索该小红书内容，再读取详情。",
            )
        texts = await self._call(
            "get_feed_detail",
            {
                "feed_id": normalized_id,
                "xsec_token": access_token,
                "load_all_comments": False,
            },
        )
        payload = self._parse_json_text(texts, "小红书详情格式无效。")
        data = payload.get("data") if isinstance(payload, dict) else None
        note = data.get("note") if isinstance(data, dict) else None
        if not isinstance(note, dict):
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="小红书详情格式无效。",
            )
        return self._normalize_detail(normalized_id, note)

    def _validated_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._budget.max_results
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Xiaohongshu result limit must be a positive integer.")
        return min(limit, self._budget.max_results)

    @staticmethod
    def _parse_json_text(texts: tuple[str, ...], safe_message: str) -> Any:
        for text in texts:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        raise PlatformError(
            platform=PlatformName.XIAOHONGSHU,
            error_type=PlatformErrorType.INVALID_RESPONSE,
            message=safe_message,
        )

    def _normalize_search_item(self, feed: dict[str, Any]) -> PlatformContent:
        content_id = str(feed["id"])
        note = feed.get("noteCard")
        note = note if isinstance(note, dict) else {}
        title = str(note.get("displayTitle") or "小红书内容").strip()[:200]
        user = note.get("user")
        author = user.get("nickname") if isinstance(user, dict) else None
        interact = note.get("interactInfo")
        metrics = self._normalize_metrics(interact)
        return PlatformContent(
            platform=self.platform,
            content_id=content_id,
            title=title,
            text="",
            source_url=(
                "https://www.xiaohongshu.com/explore/"
                f"{quote(content_id, safe='')}"
            ),
            author_name=author if isinstance(author, str) and author.strip() else None,
            metrics=metrics,
        )

    def _normalize_detail(
        self,
        content_id: str,
        note: dict[str, Any],
    ) -> PlatformContent:
        title = str(note.get("title") or "小红书内容").strip()[:200]
        description = note.get("desc")
        text = description.strip()[:4_000] if isinstance(description, str) else ""
        user = note.get("user")
        author = user.get("nickname") if isinstance(user, dict) else None
        return PlatformContent(
            platform=self.platform,
            content_id=content_id,
            title=title,
            text=text,
            source_url=(
                "https://www.xiaohongshu.com/explore/"
                f"{quote(content_id, safe='')}"
            ),
            author_name=author if isinstance(author, str) and author.strip() else None,
            metrics=self._normalize_metrics(note.get("interactInfo")),
        )

    @staticmethod
    def _normalize_metrics(value: object) -> dict[str, int | str]:
        if not isinstance(value, dict):
            return {}
        metrics: dict[str, int | str] = {}
        for source, target in (
            ("likedCount", "likes"),
            ("commentCount", "comments"),
            ("collectedCount", "favorites"),
            ("sharedCount", "shares"),
        ):
            metric = value.get(source)
            if isinstance(metric, (int, str)) and not isinstance(metric, bool):
                metrics[target] = metric
        return metrics
