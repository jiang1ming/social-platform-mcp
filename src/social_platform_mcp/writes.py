"""微博写入闭环；人工批准通过独立 CLI 完成，不注册成 MCP 工具。"""

import asyncio
import hashlib
import json
import re
from dataclasses import asdict

from .contracts import PlatformError, PlatformErrorType, normalize_required_text
from .operations import WriteResultStatus
from .write_store import WriteConflict, WriteStore


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class WriteService:
    def __init__(self, adapter, store: WriteStore):
        self.adapter, self.store = adapter, store

    async def prepare(self, kind, text, request_id, *, content_id=None, comment_id=None):
        if kind not in {"publish", "reply"}:
            raise ValueError("Invalid operation kind")
        text = normalize_required_text(text, name="text", max_length=140)
        request_id = normalize_required_text(request_id, name="request_id", max_length=160)
        if kind == "reply" and not all(isinstance(v, str) and re.fullmatch(r"[0-9]{1,128}", v) for v in (content_id, comment_id)):
            raise ValueError("Invalid reply target")
        identity = await self.adapter.get_account_identity()
        payload = {"text": text, "content_id": content_id, "comment_id": comment_id,
                   "account": identity.to_public_dict(), "public_visibility": True, "ai_generated": True}
        # 显示名可变，不参与正文/目标指纹；实际账号另以 fingerprint 绑定。
        payload["content_hash"] = digest(json.dumps({"kind": kind, **{k: v for k, v in payload.items() if k != "account"}},
                                                   ensure_ascii=False, sort_keys=True))
        item = self.store.prepare(request_id, kind, payload, digest(identity.account_id))
        return self.store.public(item)

    async def _same_account(self, item):
        identity = await self.adapter.get_account_identity()
        if digest(identity.account_id) != item["fingerprint"]:
            raise WriteConflict("account_changed")

    async def execute(self, operation_id):
        item = self.store.get(operation_id)
        if item["status"] != "approved":
            if item["status"] == "prepared":
                raise WriteConflict("human_approval_required")
            return self.store.public(item)
        await self._same_account(item)
        if not self.store.claim(operation_id):
            return self.store.public(self.store.get(operation_id))
        payload = item["payload"]
        try:
            if item["kind"] == "publish":
                result = await self.adapter.publish_text(payload["text"])
            else:
                result = await self.adapter.reply_comment(payload["content_id"], payload["comment_id"], payload["text"])
            self.store.finish(operation_id, result.status.value, result=asdict(result))
        except PlatformError as exc:
            # 即使底层适配器新增了未明确分类的失败，也不把可能已发送的请求自动重放。
            definite = exc.error_type in {PlatformErrorType.NOT_AUTHENTICATED, PlatformErrorType.PERMISSION_DENIED,
                                         PlatformErrorType.INVALID_REQUEST, PlatformErrorType.RATE_LIMITED}
            self.store.finish(operation_id, "failed" if definite else "unknown", error_code=exc.error_type.value)
        except asyncio.CancelledError:
            self.store.finish(operation_id, "unknown", error_code="execution_interrupted")
            raise
        except Exception:
            self.store.finish(operation_id, "unknown", error_code="write_unconfirmed")
        return self.store.public(self.store.get(operation_id))

    async def reconcile(self, operation_id):
        item = self.store.get(operation_id)
        if item["status"] == "confirmed":
            return self.store.public(item)
        if item["status"] not in {"running", "unknown"}:
            raise WriteConflict("reconciliation_state_conflict")
        await self._same_account(item)
        payload = item["payload"]
        if item["kind"] == "publish":
            lookup = await self.adapter.lookup_publication(expected_text=payload["text"])
            # 精确正文的匹配只提供核查证据，旧同文内容不能证明本次发送成功。
            result = {"status": "inconclusive", "lookup_status": lookup.status.value,
                      "candidate_count": lookup.candidate_count,
                      "candidate": lookup.content.to_public_dict() if lookup.content else None,
                      "reason": "text_match_is_not_operation_identity"}
        else:
            lookup = await self.adapter.lookup_reply(content_id=payload["content_id"],
                comment_id=payload["comment_id"], expected_text=payload["text"])
            result = {"status": "inconclusive", "lookup_status": lookup.status.value,
                      "candidate_count": lookup.candidate_count,
                      "candidate": asdict(lookup.reply) if lookup.reply else None,
                      "reason": "text_match_is_not_operation_identity"}
        self.store.reconcile(operation_id, result)
        return self.store.public(self.store.get(operation_id))
