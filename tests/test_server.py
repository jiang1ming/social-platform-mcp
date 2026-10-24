"""MCP 严格参数、能力和脱敏错误测试；平台调用均为 Fake。"""

import json

import pytest

from social_platform_mcp.config import Settings
from social_platform_mcp.server import create_server
from social_platform_fakes import registry


def server(tmp_path, writes=False):
    return create_server(Settings(state_dir=tmp_path), registry=registry(), writes_enabled=writes)


@pytest.mark.asyncio
async def test_discovery_annotations_and_writes_opt_in(tmp_path):
    tools = await server(tmp_path).list_tools()
    assert len(tools) == 5
    assert all(t.annotations.read_only_hint and t.annotations.open_world_hint for t in tools)
    enabled = await server(tmp_path, True).list_tools()
    assert len(enabled) == 10
    assert not any("approve" in t.name for t in enabled)
    assert next(t for t in enabled if t.name == "execute_weibo_operation").annotations.destructive_hint


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"platform": "weibo", "keyword": "咖啡", "confirmed": True},
    {"platform": "bad", "keyword": "x"}, {"platform": "weibo", "keyword": "x", "limit": True},
    {"platform": "weibo", "keyword": "x", "limit": 21}])
async def test_invalid_arguments_have_safe_error(tmp_path, arguments):
    result = await server(tmp_path).call_tool("search_platform_content", arguments)
    assert result.is_error
    assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert "confirmed" not in json.dumps(result.structured_content)


@pytest.mark.asyncio
async def test_three_searches_detail_comments_and_split_zhihu_status(tmp_path):
    app = server(tmp_path)
    for platform in ("weibo", "xiaohongshu", "zhihu"):
        result = await app.call_tool("search_platform_content", {"platform": platform, "keyword": "咖啡"})
        assert not result.is_error
        assert result.structured_content["result"]["items"][0]["source_url"].startswith("https://")
    state = await app.call_tool("check_platform_connection", {"platform": "zhihu"})
    assert state.structured_content["result"]["official_search"]["ready"]
    assert not state.structured_content["result"]["community_read"]["ready"]
    assert not (await app.call_tool("read_platform_comments", {"platform": "weibo", "content_ref": "123"})).is_error
    assert not (await app.call_tool("read_platform_comments", {"platform": "zhihu", "content_ref": "https://zhuanlan.zhihu.com/p/123"})).is_error
    assert (await app.call_tool("read_platform_comments", {"platform": "zhihu", "content_ref": "https://zhuanlan.zhihu.com/p/123", "page": 2})).is_error


@pytest.mark.asyncio
async def test_unknown_tool_and_unapproved_write_fail(tmp_path):
    app = server(tmp_path, True)
    assert (await app.call_tool("approve", {})).is_error
    prepared = await app.call_tool("prepare_weibo_publish", {"text": "正文", "request_id": "test"})
    oid = prepared.structured_content["result"]["id"]
    result = await app.call_tool("execute_weibo_operation", {"operation_id": oid})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "human_approval_required"


@pytest.mark.asyncio
async def test_unexpected_exception_does_not_leak_to_result_or_log(tmp_path, caplog):
    adapters = registry()
    async def fail(content_id):
        raise RuntimeError("secret-cookie-value")
    adapters.resolve("weibo").get_detail = fail
    app = create_server(Settings(state_dir=tmp_path), registry=adapters)
    result = await app.call_tool("get_platform_content_detail", {"platform": "weibo", "content_ref": "123"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "tool_execution_failed"
    assert "secret-cookie-value" not in json.dumps(result.structured_content)
    assert "secret-cookie-value" not in caplog.text
