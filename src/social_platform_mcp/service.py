"""平台读取入口，将平台差异转换为明确的能力与参数约束。"""

from urllib.parse import urlsplit

from .contracts import PlatformCapability, PlatformError, PlatformErrorType, PlatformName


class ReadService:
    def __init__(self, registry):
        self.registry = registry

    def info(self):
        return {"platforms": {name.value: sorted(c.value for c in self.registry.resolve(name).capabilities)
                              for name in self.registry.names()},
                "comments_platforms": ["weibo", "zhihu"],
                "notes": ["小红书详情须在本进程中先搜索；重启或令牌过期后需重新搜索。",
                          "知乎正文和评论仅支持专栏文章 URL；搜索和社区会话独立配置。",
                          "写入仅支持微博；评论定时调度由宿主 Agent 应用负责。"]}

    async def status(self, platform):
        adapter = self.registry.resolve(platform)
        if platform == "zhihu":
            result = {}
            for key, check in (("official_search", adapter.check_account),
                               ("community_read", adapter.check_community_account)):
                try:
                    result[key] = {"ok": True, **(await check()).to_public_dict()}
                except PlatformError as exc:
                    result[key] = {"ok": False, "error_code": exc.error_type.value}
            return result
        return (await adapter.check_account()).to_public_dict()

    async def search(self, platform, keyword, limit):
        adapter = self.registry.resolve(platform, capability=PlatformCapability.SEARCH)
        return (await adapter.search(keyword, limit=limit)).to_public_dict()

    @staticmethod
    def reference(platform, value):
        if not value.startswith("https://"):
            return value
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            raise ValueError("Invalid content URL")
        hosts = {"weibo": {"m.weibo.cn", "weibo.com", "www.weibo.com"},
                 "xiaohongshu": {"www.xiaohongshu.com", "xiaohongshu.com"},
                 "zhihu": {"zhuanlan.zhihu.com"}}
        if parsed.hostname not in hosts[platform]:
            raise ValueError("Invalid platform URL")
        if platform == "zhihu":
            return value
        return parsed.path.rstrip("/").split("/")[-1]

    async def detail(self, platform, content_ref):
        adapter = self.registry.resolve(platform, capability=PlatformCapability.DETAIL)
        return (await adapter.get_detail(self.reference(platform, content_ref))).to_public_dict()

    async def comments(self, platform, content_ref, limit, page):
        adapter = self.registry.resolve(platform, capability=PlatformCapability.COMMENTS)
        if platform == "zhihu":
            if page != 1:
                raise ValueError("Zhihu pagination is not supported")
            result = (await adapter.read_comments(content_ref, limit=limit)).to_public_dict()
            if len(result["items"]) > limit:
                result["items"] = result["items"][:limit]
                result["is_end"] = False
            return result
        batch = await adapter.read_post_comments(self.reference(platform, content_ref), page=page, count=limit)
        return {"platform": "weibo", "post_id": batch.post_id, "page": batch.page,
                "items": [item.to_public_dict() for item in batch.items],
                "is_end": batch.is_end, "next_page": batch.next_page}
