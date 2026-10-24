"""独立配置，只读取连接信息，不读取模型密钥或觅响数据库。"""

import os
from dataclasses import dataclass
from pathlib import Path

from .adapters.weibo import WeiboAdapter
from .adapters.xiaohongshu import XiaohongshuAdapter
from .adapters.zhihu import ZhihuAdapter
from .adapters.zhihu_community import ZhihuCommunityCliDriver
from .contracts import PlatformCallBudget
from .registry import PlatformRegistry


@dataclass(frozen=True)
class Settings:
    weibo_cli: str = "weibo-cli"
    zhihu_cli: str = "zhihu-cli"
    zhihu_community_cli: str = "zhihu"
    xiaohongshu_url: str = "http://127.0.0.1:18060/mcp"
    timeout: float = 20
    xiaohongshu_timeout: float = 45
    max_results: int = 10
    state_dir: Path = Path.home() / ".local/share/social-platform-mcp"

    @classmethod
    def from_environment(cls):
        return cls(
            weibo_cli=os.environ.get("WEIBO_CLI_PATH", "weibo-cli"),
            zhihu_cli=os.environ.get("ZHIHU_CLI_PATH", "zhihu-cli"),
            zhihu_community_cli=os.environ.get("ZHIHU_COMMUNITY_CLI_PATH", "zhihu"),
            xiaohongshu_url=os.environ.get("XIAOHONGSHU_MCP_URL", "http://127.0.0.1:18060/mcp"),
            timeout=float(os.environ.get("PLATFORM_TIMEOUT_SECONDS", "20")),
            xiaohongshu_timeout=float(os.environ.get("XIAOHONGSHU_TIMEOUT_SECONDS", "45")),
            max_results=int(os.environ.get("PLATFORM_SEARCH_MAX_RESULTS", "10")),
            state_dir=Path(os.environ.get("SOCIAL_MCP_STATE_DIR", str(cls.state_dir))).expanduser(),
        )

    def registry(self):
        budget = PlatformCallBudget(self.max_results, self.timeout)
        return PlatformRegistry([
            WeiboAdapter(cli_path=self.weibo_cli, budget=budget),
            XiaohongshuAdapter(endpoint_url=self.xiaohongshu_url,
                              budget=PlatformCallBudget(self.max_results, self.xiaohongshu_timeout)),
            ZhihuAdapter(cli_path=self.zhihu_cli, budget=budget,
                         community=ZhihuCommunityCliDriver(cli_path=self.zhihu_community_cli, budget=budget)),
        ])
