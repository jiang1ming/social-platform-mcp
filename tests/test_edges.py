"""提取后新增的连接状态、访问令牌和人工批准边界。"""

import json
from pathlib import Path

import pytest

from social_platform_mcp import __main__ as cli
from social_platform_mcp.adapters.xiaohongshu import XiaohongshuAdapter, MCPToolResponse
from social_platform_mcp.config import Settings
from social_platform_mcp.contracts import PlatformError, PlatformErrorType, PlatformComment, PlatformCommentPage
from social_platform_mcp.service import ReadService
from social_platform_mcp.write_store import WriteStore
from social_platform_mcp.writes import WriteService
from social_platform_fakes import FakeAdapter, registry


@pytest.mark.asyncio
async def test_xiaohongshu_token_is_private_and_expires(monkeypatch):
    async def caller(name, args, timeout):
        return MCPToolResponse((json.dumps({"feeds": [{"id": "123", "xsecToken": "secret-access-token", "noteCard": {}}]}),))
    adapter = XiaohongshuAdapter(caller=caller)
    result = await adapter.search("咖啡")
    assert "secret-access-token" not in json.dumps(result.to_public_dict())
    adapter._token_times["123"] -= 1_801
    with pytest.raises(PlatformError) as exc:
        await adapter.get_detail("123")
    assert exc.value.error_type == PlatformErrorType.INVALID_REQUEST
    assert not adapter._access_tokens


@pytest.mark.asyncio
async def test_mcp_comment_cap_does_not_change_recursive_adapter_contract():
    adapters = registry()
    url = "https://zhuanlan.zhihu.com/p/123"
    items = tuple(PlatformComment(adapters.resolve("zhihu").platform, str(i), url, "评论") for i in range(5))
    async def nested(*args, **kwargs):
        return PlatformCommentPage(adapters.resolve("zhihu").platform, url, items, True)
    adapters.resolve("zhihu").read_comments = nested
    result = await ReadService(adapters).comments("zhihu", url, 2, 1)
    assert len(result["items"]) == 2
    assert result["is_end"] is False
    assert len((await adapters.resolve("zhihu").read_comments(url)).items) == 5


@pytest.mark.asyncio
async def test_cli_interactive_approval_binds_exact_preview(tmp_path, monkeypatch, capsys):
    svc = WriteService(FakeAdapter(), WriteStore(tmp_path))
    item = await svc.prepare("publish", "应展示的正文", "r")
    monkeypatch.setattr(cli.sys, "argv", ["social-platform-mcp", "--state-dir", str(tmp_path), "approve", item["id"]])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "批准 " + item["id"])
    cli.main()
    assert svc.store.get(item["id"])["status"] == "approved"
    assert "应展示的正文" in capsys.readouterr().out
    assert svc.adapter.sent == 0


def test_cli_refuses_noninteractive_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["social-platform-mcp", "--state-dir", str(tmp_path), "approve", "anything"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert not (tmp_path / "operations.db").exists()
