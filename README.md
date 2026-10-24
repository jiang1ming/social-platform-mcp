# 三平台 MCP

独立 Python 包及标准 MCP stdio 服务。支持微博、小红书、知乎现有读取能力，以及微博文本发布、评论回复、结果核查。不依赖觅响、LLM Key、FastAPI 或觅响数据库。

## 安装

要求 Python 3.11+ 和 uv。直接从 GitHub 安装：

```bash
uv tool install git+https://github.com/jiang1ming/social-platform-mcp.git
social-platform-mcp --version
```

也可克隆仓库后从本地安装：

```bash
uv tool install /绝对路径/social-platform-mcp
social-platform-mcp --version
```

本包可单独复制、构建和安装。尚未发布到 PyPI，不能直接假定 `uvx social-platform-mcp` 能从公共仓库下载。

开发运行（工作目录不限）：

```bash
uv run --project /绝对路径/social-platform-mcp social-platform-mcp
```

MCP 客户端可使用下面的 GitHub 安装配置，首次运行会下载依赖。支持标准 stdio MCP 的客户端可连接，不要求使用某个 Agent 框架：

```json
{
  "mcpServers": {
    "social-platform": {
      "command": "uv",
      "args": ["tool", "run", "--from", "git+https://github.com/jiang1ming/social-platform-mcp.git", "social-platform-mcp"],
      "env": {
        "WEIBO_CLI_PATH": "/绝对路径/weibo-cli",
        "ZHIHU_CLI_PATH": "/绝对路径/zhihu-cli",
        "ZHIHU_COMMUNITY_CLI_PATH": "/绝对路径/zhihu",
        "XIAOHONGSHU_MCP_URL": "http://127.0.0.1:18060/mcp"
      }
    }
  }
}
```

本地开发时，将 `args` 改为 `["run", "--project", "/绝对路径/social-platform-mcp", "social-platform-mcp"]`。

## 连接与登录

安装本包不会自动安装平台 CLI、启动浏览器服务或登录账号。

| 平台 | 上游连接 | 账号配置与当前边界 |
|---|---|---|
| 微博 | `weibo-cli` | 使用该 CLI 所登录的账号；按其安装说明和 `--help` 登录 |
| 小红书 | 已有小红书 MCP 的 Streamable HTTP 端点 | 先启动上游服务并完成用户登录，本包只代理允许的读取工具 |
| 知乎搜索 | 官方 `zhihu-cli` | 配置使用者自己的开放平台凭据；额度由上游账号决定 |
| 知乎正文、评论 | `KrisTHL181/zhihu-cli` 的 `zhihu` 命令 | 使用者自己完成社区登录，仅支持当前适配的专栏文章 URL |

上游项目独立安装和许可适用；本包不包含它们的二进制，不代替上游授权，也不绕过额度或验证码。本文仅描述现有适配命令契约，并未重新核实各上游的最新分发地址与许可证。

可选环境变量：`PLATFORM_TIMEOUT_SECONDS`（默认 20）、`XIAOHONGSHU_TIMEOUT_SECONDS`（45）、`PLATFORM_SEARCH_MAX_RESULTS`（10，1–20）。服务不会读取 `.env` 文件；通过 MCP 客户端或进程环境传入配置。每个本地实例使用该进程用户的登录态，第一版不是多租户远程账号服务。

## 工具

默认 5 个读取工具：

- `get_platform_capabilities()`：能力与限制，不访问平台。
- `check_platform_connection(platform)`：知乎分开报告官方搜索和社区会话。
- `search_platform_content(platform, keyword, limit=10)`。
- `get_platform_content_detail(platform, content_ref)`。
- `read_platform_comments(platform, content_ref, limit=10, page=1)`：微博、知乎可用；小红书未实现独立评论接口。知乎不支持页码翻页。

微博正文建议使用数字 ID；URL 输入仅用于支持的域名和数字路径，不接受微博 base62 URL 自动转换。小红书详情使用同一进程搜索返回的 ID，访问令牌不向客户端输出；进程重启或缓存过期时须重新搜索。知乎必须传 `https://zhuanlan.zhihu.com/p/...` URL。各平台返回上限可能低于请求 limit，知乎评论楼中楼会被独立裁剪并报告不完整。

关键词搜索返回的是相关内容，不能单凭搜索结果声称它是实时热点。热点排序、品牌分析、评论分级、每五分钟调度由宿主 Agent 负责。

## 微博发布与回复

在客户端 `args` 最后添加 `--enable-writes` 后，增加 5 个工具：`prepare_weibo_publish`、`prepare_weibo_reply`、`execute_weibo_operation`、`get_weibo_operation`、`reconcile_weibo_operation`。

1. Agent 调用 prepare，得到绑定正文、账号、目标和内容 hash 的持久化预览。发布为公开可见并声明 AI 生成；文本最多 140 字符。
2. Agent 向用户展示完整预览。用户在独立交互终端运行：

   ```bash
   social-platform-mcp approve 操作ID
   ```

   核对后输入终端要求的完整批准短语。批准有效期为 10 分钟。若设置了 `--state-dir`，批准命令必须使用同一个目录。

3. Agent 调用 execute。服务再次检查账号，通过 SQLite 原子领取后只发送一次；重复 request_id 必须对应相同正文和目标。
4. 超时、异常或中断后的 `unknown` 只允许查询和核查，不会自动重发。遗留 `running` 在 180 秒后可核查。核查返回同文候选；仅凭同文不能证明是本次操作，因此仍保留 unknown，供人工判断。

批准入口不作为 MCP 工具暴露。它可以阻止只有本 MCP 工具权限的客户端自行批准，但不是针对拥有本机 shell/文件访问权限 Agent 的安全隔离；宿主仍应限制此类权限。

本包不运行觅响的品牌审查和内容策略；觅响继续在自己的 Service 中执行审查、选稿和审批。直接接入本包的应用应负责自己的内容审核。

操作默认存于 `~/.local/share/social-platform-mcp/operations.db`，可用 `SOCIAL_MCP_STATE_DIR` 或 `--state-dir` 指定。包含批准正文，请妥善保管；不存 Cookie、Access Secret 和原始异常。共享同一状态目录的多个本地客户端共享操作日志和同一账号范围。

## Python 复用与测试

```python
from social_platform_mcp.config import Settings

registry = Settings.from_environment().registry()
# 执行时才会连接平台；不要在模块导入时调用。
```

测试全部使用 Fake：

```bash
uv run --project /绝对路径/social-platform-mcp --group dev pytest /绝对路径/social-platform-mcp/tests -q
uv build /绝对路径/social-platform-mcp
```

测试包含真实 stdio 协议、进程重启、写操作重复请求、账号切换、超时、取消和核查；不执行真实发布或回复。真实账号可用性须由使用者另行检查。
