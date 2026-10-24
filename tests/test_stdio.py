"""跨进程 MCP 握手与工具调用；写操作仅注入 Fake 适配器。"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from social_platform_mcp.write_store import WriteStore

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def connect(tmp_path, *, fake=True):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))}
    if fake:
        code = ("from pathlib import Path; from social_platform_fakes import registry; "
                "from social_platform_mcp.config import Settings; "
                "from social_platform_mcp.server import create_server; "
                f"create_server(Settings(state_dir=Path({str(tmp_path)!r})),registry=registry(),writes_enabled=True).run(transport='stdio')")
        args = ["-c", code]
    else:
        args = ["-m", "social_platform_mcp", "--state-dir", str(tmp_path)]
    installed = os.environ.get("SOCIAL_MCP_INSTALLED_PYTHON")
    if installed and not fake:
        env.pop("PYTHONPATH")
        args = ["-I", *args]
    params = StdioServerParameters(command=installed if installed and not fake else sys.executable,
                                   args=args, env=env, cwd=str(tmp_path))
    async with asyncio.timeout(25):
        async with Client(params) as client:
            yield client


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    assert json.loads(result.content[0].text) == result.structured_content
    return result


@pytest.mark.asyncio
async def test_real_stdio_handshake_search_and_error(tmp_path):
    async with connect(tmp_path) as client:
        assert client.server_info.name == "social-platform"
        assert len((await client.list_tools()).tools) == 10
        for platform in ("weibo", "xiaohongshu", "zhihu"):
            result = await call(client, "search_platform_content", {"platform": platform, "keyword": "咖啡"})
            assert result.structured_content["result"]["items"]
        invalid = await call(client, "get_platform_capabilities", {"confirmed": True})
        assert invalid.is_error


@pytest.mark.asyncio
async def test_stdio_write_approval_and_process_restart(tmp_path):
    async with connect(tmp_path) as client:
        result = await call(client, "prepare_weibo_publish", {"text": "Fake 测试正文", "request_id": "r"})
        item = result.structured_content["result"]
        assert (await call(client, "execute_weibo_operation", {"operation_id": item["id"]})).is_error
        WriteStore(tmp_path).approve(item["id"], item["payload"]["content_hash"])
        assert (await call(client, "execute_weibo_operation", {"operation_id": item["id"]})).structured_content["result"]["status"] == "confirmed"
    async with connect(tmp_path) as restarted:
        assert (await call(restarted, "execute_weibo_operation", {"operation_id": item["id"]})).structured_content["result"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_production_entry_point_outside_project_without_platform_calls(tmp_path):
    async with connect(tmp_path, fake=False) as client:
        assert len((await client.list_tools()).tools) == 5
        info = (await call(client, "get_platform_capabilities")).structured_content["result"]
        assert set(info["platforms"]) == {"weibo", "xiaohongshu", "zhihu"}
        assert info["mcp_writes_enabled"] is False
        assert not (tmp_path / "operations.db").exists()
