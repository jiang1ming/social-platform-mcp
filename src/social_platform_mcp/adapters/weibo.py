"""微博 CLI 的受控读取、发布、评论和回复适配器。"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote

from social_platform_mcp.constants import (
    WEIBO_REPLY_MAX_LENGTH,
    WEIBO_TEXT_MAX_LENGTH,
)
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
from social_platform_mcp.operations import (
    OperationUncertainty,
    PlatformAccountIdentity,
    PlatformCommentBatch,
    PlatformCommentRecord,
    PlatformReplyRecord,
    PublicationLookupResult,
    PublicationWriteResult,
    ReconciliationMatch,
    ReconciliationStatus,
    ReplyLookupResult,
    ReplyWriteResult,
    WriteResultStatus,
)
from social_platform_mcp.adapters.weibo_client import (
    CommandResult,
    CommandRunner,
    WeiboCliClient,
)


_HTML_TAG = re.compile(r"<[^>]+>")


class WeiboAdapter:
    """将微博 CLI JSON 转成稳定契约，且不暴露登录令牌。"""

    platform = PlatformName.WEIBO
    capabilities = frozenset(
        {
            PlatformCapability.ACCOUNT_STATUS,
            PlatformCapability.SEARCH,
            PlatformCapability.DETAIL,
            PlatformCapability.COMMENTS,
            PlatformCapability.PUBLISH,
            PlatformCapability.REPLY,
        }
    )

    def __init__(
        self,
        *,
        cli_path: str = "weibo-cli",
        budget: PlatformCallBudget | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._budget = budget or PlatformCallBudget()
        self._client = WeiboCliClient(
            cli_path=cli_path,
            budget=self._budget,
            runner=runner,
        )

    async def _invoke(self, *arguments: str) -> Any:
        return await self._client.invoke(*arguments)

    async def _invoke_write(
        self,
        *arguments: str,
    ) -> tuple[Any | None, OperationUncertainty | None]:
        """将可能已经到达平台的失败保守标记为未知。"""

        return await self._client.invoke_write(*arguments)

    async def check_account(self) -> PlatformAccountStatus:
        payload = await self._invoke("doctor")
        if not isinstance(payload, dict):
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博账号状态格式无效。",
            )
        steps = payload.get("steps")
        authenticated = bool(steps.get("login")) if isinstance(steps, dict) else False
        return PlatformAccountStatus(
            platform=self.platform,
            authenticated=authenticated,
            ready=bool(payload.get("ready")),
        )

    async def get_account_identity(self) -> PlatformAccountIdentity:
        payload = await self._invoke("users", "show/biz")
        record = self._unwrap_record(payload, ("user", "data", "result"))
        account_id = self._identifier(record, ("idstr", "id", "uid"))
        display_name = self._first_text(
            record,
            ("screen_name", "name", "username"),
        )
        if not account_id or not display_name:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博账号身份格式无效。",
            )
        return PlatformAccountIdentity(
            platform=self.platform,
            account_id=account_id,
            display_name=display_name,
        )

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> PlatformSearchResult:
        normalized_query = normalize_required_text(
            query,
            name="Weibo search query",
            max_length=100,
        )
        requested = self._validated_limit(limit)
        payload = await self._invoke(
            "search",
            "statuses/limited",
            "--q",
            normalized_query,
            "--type",
            "1",
            "--sort",
            "hot",
            "--count",
            str(max(10, requested)),
        )
        records = self._extract_records(payload, ("statuses", "data", "items"))
        items = tuple(self._normalize_content(record) for record in records[:requested])
        return PlatformSearchResult(
            platform=self.platform,
            query=normalized_query,
            items=items,
            truncated=len(records) > requested,
        )

    async def get_detail(self, content_id: str) -> PlatformContent:
        normalized_id = self._validated_id(content_id, name="Weibo content ID")
        payload = await self._invoke(
            "statuses",
            "show_batch/biz",
            "--ids",
            normalized_id,
            "--isGetLongText",
            "1",
        )
        records = self._extract_records(payload, ("statuses", "data", "items"))
        if not records:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.NOT_FOUND,
                message="未找到指定微博。",
            )
        return self._normalize_content(records[0])

    async def publish_text(self, text: str) -> PublicationWriteResult:
        normalized_text = self._validated_write_text(
            text,
            name="Weibo publication text",
            max_length=WEIBO_TEXT_MAX_LENGTH,
        )
        payload, uncertainty = await self._invoke_write(
            "statuses",
            "update",
            "--status",
            normalized_text,
            "--visible",
            "0",
            "--is_longtext",
            "0",
            "--mblog_statement",
            "1",
        )
        if uncertainty is not None:
            return PublicationWriteResult.unknown(
                platform=self.platform,
                reason=uncertainty,
            )
        try:
            record = self._unwrap_record(payload, ("status", "data", "result"))
            content = self._normalize_content(record)
        except PlatformError as exc:
            if exc.error_type is not PlatformErrorType.INVALID_RESPONSE:
                raise
            return PublicationWriteResult.unknown(
                platform=self.platform,
                reason=OperationUncertainty.INVALID_RESPONSE,
            )
        return PublicationWriteResult(
            platform=self.platform,
            status=WriteResultStatus.CONFIRMED,
            content_id=content.content_id,
            content_url=content.source_url,
        )

    async def lookup_publication(
        self,
        *,
        content_id: str | None = None,
        expected_text: str | None = None,
    ) -> PublicationLookupResult:
        if content_id is None and expected_text is None:
            raise ValueError("Publication lookup requires an ID or expected text.")
        if content_id is not None:
            try:
                content = await self.get_detail(content_id)
            except PlatformError as exc:
                if exc.error_type is PlatformErrorType.NOT_FOUND:
                    return PublicationLookupResult(
                        platform=self.platform,
                        status=ReconciliationStatus.NOT_FOUND,
                    )
                if self._is_inconclusive_error(exc):
                    return PublicationLookupResult(
                        platform=self.platform,
                        status=ReconciliationStatus.INCONCLUSIVE,
                    )
                raise
            return PublicationLookupResult(
                platform=self.platform,
                status=ReconciliationStatus.FOUND,
                content=content,
                matched_by=ReconciliationMatch.PLATFORM_ID,
                candidate_count=1,
            )

        normalized_text = self._validated_write_text(
            expected_text,
            name="Expected Weibo publication text",
            max_length=WEIBO_TEXT_MAX_LENGTH,
        )
        try:
            payload = await self._invoke(
                "statuses",
                "user_timeline/biz",
                "--page",
                "1",
                "--count",
                "20",
                "--feature",
                "7",
                "--base_app",
                "1",
                "--trim_user",
                "1",
            )
            records = self._extract_records(payload, ("statuses", "data", "items"))
            matches = tuple(
                self._normalize_content(record)
                for record in records
                if self._plain_text(
                    record.get("longText")
                    or record.get("long_text")
                    or record.get("text")
                )
                == normalized_text
            )
        except PlatformError as exc:
            if self._is_inconclusive_error(exc):
                return PublicationLookupResult(
                    platform=self.platform,
                    status=ReconciliationStatus.INCONCLUSIVE,
                )
            raise
        if len(matches) == 1:
            return PublicationLookupResult(
                platform=self.platform,
                status=ReconciliationStatus.FOUND,
                content=matches[0],
                matched_by=ReconciliationMatch.EXACT_TEXT,
                candidate_count=1,
            )
        return PublicationLookupResult(
            platform=self.platform,
            status=(
                ReconciliationStatus.NOT_FOUND
                if not matches
                else ReconciliationStatus.INCONCLUSIVE
            ),
            candidate_count=len(matches),
        )

    async def read_post_comments(
        self,
        content_id: str,
        *,
        page: int = 1,
        count: int = 50,
        since_id: str | None = None,
        max_id: str | None = None,
    ) -> PlatformCommentBatch:
        normalized_post_id = self._validated_id(
            content_id,
            name="Weibo content ID",
        )
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("Weibo comment page must be a positive integer.")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 200
        ):
            raise ValueError("Weibo comment count must be between 1 and 200.")
        arguments = [
            "comments",
            "show/biz",
            "--id",
            normalized_post_id,
            "--page",
            str(page),
            "--count",
            str(count),
            "--filter_by_author",
            "0",
        ]
        if since_id is not None:
            arguments.extend(
                ("--since_id", self._validated_id(since_id, name="Since comment ID"))
            )
        if max_id is not None:
            arguments.extend(
                ("--max_id", self._validated_id(max_id, name="Maximum comment ID"))
            )
        payload = await self._invoke(*arguments)
        records = self._extract_records(payload, ("comments", "data", "items"))
        items = tuple(
            self._normalize_comment(record, post_id=normalized_post_id)
            for record in records
        )
        is_end = self._comment_page_is_end(
            payload,
            page=page,
            count=count,
            returned=len(items),
        )
        return PlatformCommentBatch(
            platform=self.platform,
            post_id=normalized_post_id,
            page=page,
            count=count,
            items=items,
            is_end=is_end,
            next_page=None if is_end else page + 1,
        )

    async def reply_comment(
        self,
        content_id: str,
        comment_id: str,
        text: str,
    ) -> ReplyWriteResult:
        normalized_post_id = self._validated_id(
            content_id,
            name="Weibo content ID",
        )
        normalized_comment_id = self._validated_id(
            comment_id,
            name="Weibo comment ID",
        )
        normalized_text = self._validated_write_text(
            text,
            name="Weibo reply text",
            max_length=WEIBO_REPLY_MAX_LENGTH,
        )
        payload, uncertainty = await self._invoke_write(
            "comments",
            "reply",
            "--id",
            normalized_post_id,
            "--cid",
            normalized_comment_id,
            "--comment",
            normalized_text,
            "--is_repost",
            "0",
            "--comment_ori",
            "0",
            "--without_mention",
            "1",
        )
        if uncertainty is not None:
            return ReplyWriteResult.unknown(
                platform=self.platform,
                post_id=normalized_post_id,
                parent_comment_id=normalized_comment_id,
                reason=uncertainty,
            )
        try:
            record = self._unwrap_record(payload, ("comment", "data", "result"))
            reply_id = self._identifier(record, ("idstr", "id", "cid"))
            if not reply_id:
                raise PlatformError(
                    platform=self.platform,
                    error_type=PlatformErrorType.INVALID_RESPONSE,
                    message="微博回复结果缺少可用 ID。",
                )
        except PlatformError as exc:
            if exc.error_type is not PlatformErrorType.INVALID_RESPONSE:
                raise
            return ReplyWriteResult.unknown(
                platform=self.platform,
                post_id=normalized_post_id,
                parent_comment_id=normalized_comment_id,
                reason=OperationUncertainty.INVALID_RESPONSE,
            )
        return ReplyWriteResult(
            platform=self.platform,
            post_id=normalized_post_id,
            parent_comment_id=normalized_comment_id,
            status=WriteResultStatus.CONFIRMED,
            reply_id=reply_id,
        )

    async def lookup_reply(
        self,
        *,
        reply_id: str | None = None,
        content_id: str | None = None,
        comment_id: str | None = None,
        expected_text: str | None = None,
    ) -> ReplyLookupResult:
        if reply_id is None and expected_text is None:
            raise ValueError("Reply lookup requires a reply ID or expected text.")
        try:
            if reply_id is not None:
                normalized_reply_id = self._validated_id(
                    reply_id,
                    name="Weibo reply ID",
                )
                payload = await self._invoke(
                    "comments",
                    "show_batch/biz",
                    "--cids",
                    normalized_reply_id,
                )
                records = self._extract_records(
                    payload,
                    ("comments", "data", "items"),
                )
                matches = tuple(
                    self._normalize_reply_record(record)
                    for record in records
                    if self._identifier(record, ("idstr", "id", "cid"))
                    == normalized_reply_id
                )
                matched_by = ReconciliationMatch.PLATFORM_ID
            else:
                normalized_post_id = (
                    self._validated_id(content_id, name="Weibo content ID")
                    if content_id is not None
                    else None
                )
                normalized_comment_id = (
                    self._validated_id(comment_id, name="Weibo comment ID")
                    if comment_id is not None
                    else None
                )
                normalized_text = self._validated_write_text(
                    expected_text,
                    name="Expected Weibo reply text",
                    max_length=WEIBO_REPLY_MAX_LENGTH,
                )
                payload = await self._invoke(
                    "comments",
                    "by_me/biz",
                    "--page",
                    "1",
                    "--count",
                    "50",
                    "--filter_by_source",
                    "1",
                )
                records = self._extract_records(
                    payload,
                    ("comments", "data", "items"),
                )
                candidates = tuple(
                    self._normalize_reply_record(
                        record,
                        fallback_post_id=normalized_post_id,
                        fallback_parent_id=normalized_comment_id,
                    )
                    for record in records
                    if self._plain_text(record.get("text")) == normalized_text
                )
                matches = tuple(
                    item
                    for item in candidates
                    if (
                        normalized_post_id is None
                        or item.post_id == normalized_post_id
                    )
                    and (
                        normalized_comment_id is None
                        or item.parent_comment_id == normalized_comment_id
                    )
                )
                matched_by = ReconciliationMatch.EXACT_TEXT
        except PlatformError as exc:
            if self._is_inconclusive_error(exc):
                return ReplyLookupResult(
                    platform=self.platform,
                    status=ReconciliationStatus.INCONCLUSIVE,
                )
            raise
        if len(matches) == 1:
            return ReplyLookupResult(
                platform=self.platform,
                status=ReconciliationStatus.FOUND,
                reply=matches[0],
                matched_by=matched_by,
                candidate_count=1,
            )
        return ReplyLookupResult(
            platform=self.platform,
            status=(
                ReconciliationStatus.NOT_FOUND
                if not matches
                else ReconciliationStatus.INCONCLUSIVE
            ),
            candidate_count=len(matches),
        )

    def _validated_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._budget.max_results
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Weibo result limit must be a positive integer.")
        return min(limit, self._budget.max_results)

    @staticmethod
    def _validated_id(value: object, *, name: str) -> str:
        normalized = normalize_required_text(value, name=name, max_length=64)
        if not normalized.isdecimal():
            raise ValueError(f"{name} must contain only decimal digits.")
        return normalized

    @staticmethod
    def _validated_write_text(
        value: object,
        *,
        name: str,
        max_length: int,
    ) -> str:
        return normalize_required_text(value, name=name, max_length=max_length)

    @staticmethod
    def _is_inconclusive_error(error: PlatformError) -> bool:
        return error.error_type in {
            PlatformErrorType.TIMEOUT,
            PlatformErrorType.UNAVAILABLE,
            PlatformErrorType.INVALID_RESPONSE,
        }

    @classmethod
    def _unwrap_record(
        cls,
        payload: Any,
        keys: tuple[str, ...],
    ) -> dict[str, Any]:
        candidate = payload
        for _ in range(4):
            if not isinstance(candidate, dict):
                break
            if any(
                key in candidate
                for key in (
                    "idstr",
                    "id",
                    "uid",
                    "cid",
                    "screen_name",
                    "text",
                )
            ):
                return candidate
            for key in keys:
                nested = candidate.get(key)
                if isinstance(nested, dict):
                    candidate = nested
                    break
            else:
                return candidate
        if not isinstance(candidate, dict):
            raise PlatformError(
                platform=PlatformName.WEIBO,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博返回对象格式无效。",
            )
        return candidate

    @staticmethod
    def _identifier(record: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = record.get(key)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (str, int)):
                normalized = str(value).strip()
                if normalized:
                    return normalized
        return ""

    @staticmethod
    def _first_text(record: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _extract_records(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        candidate = payload
        for _ in range(4):
            if not isinstance(candidate, dict):
                break
            for key in keys:
                value = candidate.get(key)
                if isinstance(value, list):
                    candidate = value
                    break
            else:
                if "text" in candidate and any(
                    key in candidate for key in ("idstr", "id", "mid", "cid")
                ):
                    candidate = [candidate]
                    break
                nested = next(
                    (
                        candidate[key]
                        for key in ("data", "result")
                        if isinstance(candidate.get(key), dict)
                    ),
                    None,
                )
                if nested is None:
                    break
                candidate = nested
                continue
            break
        if not isinstance(candidate, list):
            raise PlatformError(
                platform=PlatformName.WEIBO,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博内容列表格式无效。",
            )
        return [item for item in candidate if isinstance(item, dict)]

    @staticmethod
    def _plain_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return html.unescape(_HTML_TAG.sub("", value)).strip()[:4_000]

    def _normalize_comment(
        self,
        record: dict[str, Any],
        *,
        post_id: str,
    ) -> PlatformCommentRecord:
        comment_id = self._identifier(record, ("idstr", "id", "cid"))
        if not comment_id:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博评论缺少可用 ID。",
            )
        user = record.get("user")
        author_id = (
            self._identifier(user, ("idstr", "id", "uid"))
            if isinstance(user, dict)
            else ""
        )
        author_name = (
            self._first_text(user, ("screen_name", "name", "username"))
            if isinstance(user, dict)
            else ""
        )
        parent_id = self._identifier(record, ("rootidstr", "rootid", "root_id"))
        reply_comment = record.get("reply_comment") or record.get(
            "reply_original_comment"
        )
        if isinstance(reply_comment, dict):
            parent_id = self._identifier(
                reply_comment,
                ("idstr", "id", "cid"),
            ) or parent_id
        return PlatformCommentRecord(
            platform=self.platform,
            post_id=post_id,
            comment_id=comment_id,
            text=self._plain_text(record.get("text")),
            content_url=(
                f"https://m.weibo.cn/detail/{quote(post_id, safe='')}"
                f"#comment-{quote(comment_id, safe='')}"
            ),
            author_account_id=author_id or None,
            author_name=author_name or None,
            parent_id=parent_id or None,
            created_at=(
                record.get("created_at")
                if isinstance(record.get("created_at"), str)
                else None
            ),
        )

    def _normalize_reply_record(
        self,
        record: dict[str, Any],
        *,
        fallback_post_id: str | None = None,
        fallback_parent_id: str | None = None,
    ) -> PlatformReplyRecord:
        reply_id = self._identifier(record, ("idstr", "id", "cid"))
        status = record.get("status")
        post_id = (
            self._identifier(status, ("idstr", "id", "mid"))
            if isinstance(status, dict)
            else ""
        ) or (fallback_post_id or "")
        reply_comment = record.get("reply_comment") or record.get(
            "reply_original_comment"
        )
        parent_id = (
            self._identifier(reply_comment, ("idstr", "id", "cid"))
            if isinstance(reply_comment, dict)
            else ""
        ) or self._identifier(record, ("rootidstr", "rootid", "root_id"))
        parent_id = parent_id or fallback_parent_id
        if not reply_id or not post_id:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博回复核查结果格式无效。",
            )
        return PlatformReplyRecord(
            platform=self.platform,
            post_id=post_id,
            parent_comment_id=parent_id,
            reply_id=reply_id,
            text=self._plain_text(record.get("text")),
        )

    @staticmethod
    def _comment_page_is_end(
        payload: Any,
        *,
        page: int,
        count: int,
        returned: int,
    ) -> bool:
        if isinstance(payload, dict):
            total = payload.get("total_number") or payload.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                return page * count >= total
            next_page = payload.get("next_page")
            if isinstance(next_page, int) and not isinstance(next_page, bool):
                return next_page <= page
            next_cursor = payload.get("next_cursor")
            if isinstance(next_cursor, int) and not isinstance(next_cursor, bool):
                return next_cursor <= 0
        return returned < count

    def _normalize_content(self, record: dict[str, Any]) -> PlatformContent:
        content_id = str(
            record.get("idstr") or record.get("id") or record.get("mid") or ""
        ).strip()
        if not content_id:
            raise PlatformError(
                platform=self.platform,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博内容缺少可用 ID。",
            )
        long_text = record.get("longText") or record.get("long_text")
        if isinstance(long_text, dict):
            long_text = long_text.get("content") or long_text.get("text")
        text = self._plain_text(long_text or record.get("text"))
        user = record.get("user")
        author = user.get("screen_name") if isinstance(user, dict) else None
        metrics: dict[str, int | str] = {}
        for source, target in (
            ("reposts_count", "reposts"),
            ("comments_count", "comments"),
            ("attitudes_count", "likes"),
        ):
            value = record.get(source)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                metrics[target] = value
        return PlatformContent(
            platform=self.platform,
            content_id=content_id,
            title=(text[:80] or "微博内容"),
            text=text,
            source_url=f"https://m.weibo.cn/detail/{quote(content_id, safe='')}",
            author_name=author if isinstance(author, str) and author.strip() else None,
            metrics=metrics,
        )
