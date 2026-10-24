"""可注入 MCP 服务的假平台；不包含网络或真实写操作。"""

import asyncio

from social_platform_mcp.contracts import (PlatformName, PlatformCapability, PlatformAccountStatus,
    PlatformContent, PlatformSearchResult, PlatformCommentPage)
from social_platform_mcp.operations import (PlatformAccountIdentity, PublicationWriteResult,
    ReplyWriteResult, WriteResultStatus, OperationUncertainty, PublicationLookupResult,
    ReconciliationStatus, PlatformCommentBatch)
from social_platform_mcp.registry import PlatformRegistry


class FakeAdapter:
    def __init__(self, platform="weibo"):
        self.platform = PlatformName(platform)
        self.capabilities = frozenset(PlatformCapability)
        self.account_id = "123456"
        self.display_name = "测试账号"
        self.sent = 0
        self.unknown = False
        self.fail = False

    async def check_account(self):
        return PlatformAccountStatus(self.platform, True, True)

    async def check_community_account(self):
        return PlatformAccountStatus(self.platform, False, False)

    async def get_account_identity(self):
        await asyncio.sleep(0)
        return PlatformAccountIdentity(self.platform, self.account_id, self.display_name)

    async def search(self, query, *, limit=None):
        return PlatformSearchResult(self.platform, query, (await self.get_detail("123"),))

    async def get_detail(self, content_id):
        return PlatformContent(self.platform, "123", "测试标题", "测试正文", "https://example.com/123")

    async def read_comments(self, content_url, *, limit=10):
        return PlatformCommentPage(self.platform, content_url, (), True)

    async def read_post_comments(self, content_id, **kwargs):
        return PlatformCommentBatch(self.platform, content_id, kwargs.get("page", 1), kwargs.get("count", 10), (), True)

    async def publish_text(self, text):
        self.sent += 1
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("private-key-do-not-log")
        if self.unknown:
            return PublicationWriteResult.unknown(platform=self.platform, reason=OperationUncertainty.TIMEOUT)
        return PublicationWriteResult(self.platform, WriteResultStatus.CONFIRMED, "123", "https://weibo.com/123")

    async def reply_comment(self, content_id, comment_id, text):
        self.sent += 1
        await asyncio.sleep(0)
        return ReplyWriteResult(self.platform, content_id, comment_id, WriteResultStatus.CONFIRMED, "789")

    async def lookup_publication(self, **kwargs):
        return PublicationLookupResult(self.platform, ReconciliationStatus.NOT_FOUND)


def registry():
    return PlatformRegistry([FakeAdapter(p) for p in PlatformName])
