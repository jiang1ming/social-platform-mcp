"""KrisTHL181/zhihu-cli 的受限只读驱动。

该驱动只允许账号检查、专栏文章读取和评论读取，不向业务层暴露任意命令执行。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from social_platform_mcp.contracts import (
    PlatformAccountStatus,
    PlatformCallBudget,
    PlatformComment,
    PlatformCommentPage,
    PlatformContent,
    PlatformError,
    PlatformErrorType,
    PlatformName,
    normalize_required_text,
)


_MAX_COMMAND_OUTPUT_BYTES = 4_000_000
_ARTICLE_PATH = re.compile(r"^/p/(?P<article_id>\d+)/?$")
_INCOMPLETE_WARNING = re.compile(
    r"reported\s+\d+\s+total\s+items\s+but\s+only\s+\d+\s+were\s+returned",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ZhihuCommunityCommandResult:
    """可替换的第三方知乎 CLI 运行结果。"""

    returncode: int
    stdout: str
    stderr: str = ""


ZhihuCommunityCommandRunner = Callable[
    [Sequence[str], float],
    Awaitable[ZhihuCommunityCommandResult],
]


async def _run_command(
    arguments: Sequence[str],
    timeout: float,
) -> ZhihuCommunityCommandResult:
    """以参数数组运行第三方 CLI，避免经过 shell 解释。"""

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
            message="知乎社区 CLI 不可用。",
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
            message="知乎社区 CLI 读取超时。",
            retryable=True,
        ) from exc

    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=PlatformErrorType.INVALID_RESPONSE,
            message="知乎社区 CLI 返回内容超过安全上限。",
        )
    return ZhihuCommunityCommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class ZhihuCommunityCliDriver:
    """把第三方 CLI 的文章和评论数据转换为项目统一契约。"""

    def __init__(
        self,
        *,
        cli_path: str = "zhihu",
        budget: PlatformCallBudget | None = None,
        runner: ZhihuCommunityCommandRunner | None = None,
    ) -> None:
        self._cli_path = normalize_required_text(
            cli_path,
            name="Zhihu community CLI path",
            max_length=1_024,
        )
        self._budget = budget or PlatformCallBudget()
        self._runner = runner or _run_command

    async def check_account(self) -> PlatformAccountStatus:
        """检查第三方 CLI 是否持有可回读的登录会话。"""

        payload, _ = await self._invoke("auth", "status", "--json")
        if not isinstance(payload, dict):
            self._raise_invalid_response("知乎社区 CLI 账号状态格式无效。")
        has_cookie = payload.get("has_cookie") is True
        username = payload.get("username")
        ready = has_cookie and isinstance(username, str) and bool(username.strip())
        return PlatformAccountStatus(PlatformName.ZHIHU, has_cookie, ready)

    async def get_article(self, content_url: str) -> PlatformContent:
        """根据官方搜索返回的专栏 URL 读取正文和公开指标。"""

        canonical_url, article_id = self._canonical_article_url(content_url)
        payload, _ = await self._invoke(
            "browse",
            "article",
            canonical_url,
            "--no-reading-mode",
            "--json",
        )
        if not isinstance(payload, dict):
            self._raise_invalid_response("知乎社区 CLI 文章格式无效。")
        metadata = payload.get("metadata")
        body = payload.get("content_md")
        if not isinstance(metadata, dict) or not isinstance(body, str):
            self._raise_invalid_response("知乎社区 CLI 文章格式无效。")

        title = self._optional_text(metadata.get("title")) or "知乎文章"
        author = metadata.get("author")
        author_name = (
            self._optional_text(author.get("name"))
            if isinstance(author, dict)
            else self._optional_text(author)
        )
        returned_url = self._optional_text(metadata.get("url")) or canonical_url
        try:
            normalized_returned_url, returned_id = self._canonical_article_url(
                returned_url
            )
        except ValueError as exc:
            raise PlatformError(
                platform=PlatformName.ZHIHU,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="知乎社区 CLI 返回了非法文章链接。",
            ) from exc
        if returned_id != article_id:
            self._raise_invalid_response("知乎社区 CLI 返回了其他文章。")

        metrics = self._article_metrics(metadata)
        return PlatformContent(
            PlatformName.ZHIHU,
            article_id,
            title[:200],
            body[:20_000],
            normalized_returned_url,
            author_name=author_name,
            metrics=metrics,
        )

    async def read_comments(
        self,
        content_url: str,
        *,
        limit: int = 20,
    ) -> PlatformCommentPage:
        """读取有上限的根评论，并递归展开 CLI 返回的楼中楼。"""

        canonical_url, _ = self._canonical_article_url(content_url)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Zhihu comment limit must be between 1 and 50.")
        bounded_limit = min(limit, self._budget.max_results)
        payload, stderr = await self._invoke(
            "browse",
            "comments",
            canonical_url,
            "--limit",
            str(bounded_limit),
            "--max",
            str(bounded_limit),
            "--sort",
            "time",
            "--json",
        )
        if not isinstance(payload, list):
            self._raise_invalid_response("知乎社区 CLI 评论列表格式无效。")

        items: list[PlatformComment] = []
        root_count = 0
        for root in payload:
            if not isinstance(root, dict):
                continue
            normalized_root = self._normalize_comment(root, canonical_url)
            if normalized_root is None:
                continue
            root_count += 1
            items.append(normalized_root)
            self._append_children(
                items,
                root.get("child_comments"),
                canonical_url,
                fallback_parent_id=normalized_root.comment_id,
            )

        incomplete = root_count >= bounded_limit or bool(
            _INCOMPLETE_WARNING.search(stderr)
        )
        return PlatformCommentPage(
            PlatformName.ZHIHU,
            canonical_url,
            tuple(items),
            is_end=not incomplete,
            next_offset=None,
        )

    async def _invoke(self, *arguments: str) -> tuple[Any, str]:
        result = await self._runner(
            (self._cli_path, *arguments),
            self._budget.timeout_seconds,
        )
        if result.returncode != 0:
            self._raise_cli_error(result.stderr or result.stdout)
        try:
            return json.loads(result.stdout), result.stderr
        except json.JSONDecodeError as exc:
            raise PlatformError(
                platform=PlatformName.ZHIHU,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="知乎社区 CLI 返回了无效 JSON。",
            ) from exc

    def _raise_cli_error(self, raw_message: str) -> None:
        message = raw_message.casefold()
        if any(
            token in message
            for token in (
                "no headers cached",
                "no cookie",
                "login required",
                "not authenticated",
            )
        ):
            error_type = PlatformErrorType.NOT_AUTHENTICATED
            safe_message = "知乎社区 CLI 尚未登录或会话已失效。"
            retryable = False
        elif any(token in message for token in ("403", "captcha", "verification")):
            error_type = PlatformErrorType.PERMISSION_DENIED
            safe_message = "知乎限制了本次社区读取，请稍后由用户检查账号状态。"
            retryable = False
        elif any(token in message for token in ("429", "rate limit", "too many")):
            error_type = PlatformErrorType.RATE_LIMITED
            safe_message = "知乎社区读取频率受限。"
            retryable = False
        elif "timeout" in message or "timed out" in message:
            error_type = PlatformErrorType.TIMEOUT
            safe_message = "知乎社区 CLI 读取超时。"
            retryable = True
        else:
            error_type = PlatformErrorType.UNAVAILABLE
            safe_message = "知乎社区 CLI 当前不可用。"
            retryable = True
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=error_type,
            message=safe_message,
            retryable=retryable,
        )

    def _normalize_comment(
        self,
        record: dict[str, Any],
        content_url: str,
        *,
        fallback_parent_id: str | None = None,
    ) -> PlatformComment | None:
        comment_id = self._optional_text(record.get("id"))
        text = self._optional_text(record.get("content"))
        if comment_id is None or text is None:
            return None
        parent_id = (
            self._optional_text(record.get("reply_comment_id"))
            or fallback_parent_id
        )
        return PlatformComment(
            PlatformName.ZHIHU,
            comment_id,
            content_url,
            text[:10_000],
            author_name=self._optional_text(record.get("author")),
            parent_id=parent_id,
            created_at=self._optional_text(record.get("created_time")),
        )

    def _append_children(
        self,
        output: list[PlatformComment],
        children: object,
        content_url: str,
        *,
        fallback_parent_id: str,
    ) -> None:
        if not isinstance(children, list):
            return
        for child in children:
            if not isinstance(child, dict):
                continue
            normalized = self._normalize_comment(
                child,
                content_url,
                fallback_parent_id=fallback_parent_id,
            )
            if normalized is None:
                continue
            output.append(normalized)
            self._append_children(
                output,
                child.get("child_comments"),
                content_url,
                fallback_parent_id=normalized.comment_id,
            )

    @staticmethod
    def _article_metrics(metadata: dict[str, Any]) -> dict[str, int | str]:
        metrics: dict[str, int | str] = {}
        stats = metadata.get("stats")
        if isinstance(stats, dict):
            for source, target in (
                ("voteup_count", "voteup_count"),
                ("comment_count", "comment_count"),
                ("favlists_count", "favorite_count"),
            ):
                value = stats.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    metrics[target] = value
        for source, target in (
            ("created_time", "created_at"),
            ("updated_time", "updated_at"),
            ("comment_permission", "comment_permission"),
        ):
            value = metadata.get(source)
            if isinstance(value, str) and value.strip():
                metrics[target] = value.strip()
        return metrics

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    @staticmethod
    def _canonical_article_url(value: object) -> tuple[str, str]:
        normalized = normalize_required_text(
            value,
            name="Zhihu article URL",
            max_length=1_024,
        )
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "zhuanlan.zhihu.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Zhihu article URL must use the approved HTTPS host.")
        match = _ARTICLE_PATH.fullmatch(parsed.path)
        if match is None:
            raise ValueError("Zhihu article URL must point to a column article.")
        article_id = match.group("article_id")
        return f"https://zhuanlan.zhihu.com/p/{article_id}", article_id

    @staticmethod
    def _raise_invalid_response(message: str) -> None:
        raise PlatformError(
            platform=PlatformName.ZHIHU,
            error_type=PlatformErrorType.INVALID_RESPONSE,
            message=message,
        )
