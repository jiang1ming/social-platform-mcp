"""验证批准、竞争、重启、取消和未知结果均不造成重复写入。"""

import asyncio
import json
import sqlite3
import time

import pytest

from social_platform_mcp.write_store import WriteStore, WriteConflict
from social_platform_mcp.writes import WriteService
from social_platform_fakes import FakeAdapter


def service(tmp_path, adapter=None):
    return WriteService(adapter or FakeAdapter(), WriteStore(tmp_path))


def approve(svc, item):
    svc.store.approve(item["id"], item["payload"]["content_hash"])


@pytest.mark.asyncio
async def test_publish_requires_approval_and_survives_restart(tmp_path):
    svc = service(tmp_path)
    item = await svc.prepare("publish", "批准的正文", "r1")
    with pytest.raises(WriteConflict, match="human_approval_required"):
        await svc.execute(item["id"])
    assert svc.adapter.sent == 0
    approve(svc, item)
    assert (await svc.execute(item["id"]))["status"] == "confirmed"
    restored = service(tmp_path, svc.adapter)
    assert (await restored.execute(item["id"]))["status"] == "confirmed"
    assert svc.adapter.sent == 1


@pytest.mark.asyncio
async def test_prepare_is_idempotent_but_different_body_conflicts(tmp_path):
    svc = service(tmp_path)
    first = await svc.prepare("publish", "正文", "r")
    assert (await svc.prepare("publish", "正文", "r"))["id"] == first["id"]
    with pytest.raises(WriteConflict, match="request_id_conflict"):
        await svc.prepare("publish", "不同正文", "r")
    with pytest.raises(WriteConflict, match="snapshot_changed"):
        svc.store.approve(first["id"], "wrong-hash")


@pytest.mark.asyncio
async def test_account_changed_and_expired_approval_do_not_send(tmp_path):
    svc = service(tmp_path)
    item = await svc.prepare("publish", "正文", "r")
    approve(svc, item)
    svc.adapter.account_id = "999999"
    with pytest.raises(WriteConflict, match="account_changed"):
        await svc.execute(item["id"])
    svc.adapter.account_id = "123456"
    with svc.store.transaction() as db:
        db.execute("UPDATE operations SET approved=?", (time.time()-601,))
    with pytest.raises(WriteConflict, match="approval_expired"):
        await svc.execute(item["id"])
    assert svc.adapter.sent == 0


@pytest.mark.asyncio
async def test_two_services_can_only_claim_once(tmp_path):
    svc = service(tmp_path)
    other = service(tmp_path, svc.adapter)
    item = await svc.prepare("reply", "回复", "r", content_id="123", comment_id="456")
    approve(svc, item)
    results = await asyncio.gather(svc.execute(item["id"]), other.execute(item["id"]))
    assert all(r["status"] in {"running", "confirmed"} for r in results)
    assert svc.adapter.sent == 1
    assert svc.store.get(item["id"])["result"]["parent_comment_id"] == "456"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_unknown_is_not_replayed_or_mistaken_for_confirmed(tmp_path, failure):
    svc = service(tmp_path)
    svc.adapter.unknown = failure == "timeout"
    svc.adapter.fail = failure == "exception"
    item = await svc.prepare("publish", "正文", "r")
    approve(svc, item)
    assert (await svc.execute(item["id"]))["status"] == "unknown"
    assert (await svc.execute(item["id"]))["status"] == "unknown"
    evidence = await svc.reconcile(item["id"])
    assert evidence["status"] == "unknown"
    assert svc.adapter.sent == 1
    assert "private-key" not in json.dumps(evidence)
    assert "private-key" not in svc.store.path.read_bytes().decode("utf-8", errors="ignore")
    svc.adapter.display_name = "改名后的同一账号"
    with pytest.raises(WriteConflict, match="unresolved_matching_operation"):
        await svc.prepare("publish", "正文", "another-request-id")


@pytest.mark.asyncio
async def test_cancelled_and_crashed_running_are_not_replayed(tmp_path):
    svc = service(tmp_path)
    item = await svc.prepare("publish", "正文", "r")
    approve(svc, item)
    started = asyncio.Event()
    async def blocked(text):
        svc.adapter.sent += 1
        started.set()
        await asyncio.Event().wait()
    svc.adapter.publish_text = blocked
    task = asyncio.create_task(svc.execute(item["id"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert svc.store.get(item["id"])["status"] == "unknown"
    assert (await service(tmp_path, svc.adapter).execute(item["id"]))["status"] == "unknown"
    assert svc.adapter.sent == 1
    # 模拟进程在领取后退出：新进程不重新发送，等待窗口后只读核查。
    other = await svc.prepare("publish", "另一篇", "r2")
    approve(svc, other)
    assert svc.store.claim(other["id"])
    assert (await svc.execute(other["id"]))["status"] == "running"
    with pytest.raises(WriteConflict, match="operation_running"):
        await svc.reconcile(other["id"])
    with svc.store.transaction() as db:
        db.execute("UPDATE operations SET started=? WHERE id=?", (time.time()-181, other["id"]))
    assert (await svc.reconcile(other["id"]))["status"] == "unknown"


@pytest.mark.asyncio
async def test_preexisting_second_approval_cannot_replay_unknown_text(tmp_path):
    svc = service(tmp_path)
    svc.adapter.unknown = True
    one = await svc.prepare("publish", "相同正文", "one")
    two = await svc.prepare("publish", "相同正文", "two")
    approve(svc, one)
    approve(svc, two)
    await svc.execute(one["id"])
    with pytest.raises(WriteConflict, match="unresolved_matching_operation"):
        await svc.execute(two["id"])
    assert svc.adapter.sent == 1
