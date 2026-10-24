"""SQLite 写操作日志：批准绑定快照，条件更新防止重复发送。"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class WriteConflict(ValueError):
    """仅携带固定错误码，不保存外部异常正文。"""


class WriteStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "operations.db"
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, approved REAL,
                started REAL, result TEXT, error_code TEXT)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise WriteConflict("operation_not_found")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def get(self, operation_id):
        with self.transaction() as db:
            return self.decode(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def prepare(self, request_id, kind, payload, fingerprint):
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["kind"] != kind or existing["payload"] != rendered or existing["fingerprint"] != fingerprint:
                    raise WriteConflict("request_id_conflict")
                return self.decode(existing)
            uncertain = db.execute("SELECT id FROM operations WHERE kind=? AND json_extract(payload,'$.content_hash')=? AND fingerprint=? AND status IN ('running','unknown')",
                                   (kind, payload["content_hash"], fingerprint)).fetchone()
            if uncertain:
                raise WriteConflict("unresolved_matching_operation")
            oid = str(uuid4())
            db.execute("INSERT INTO operations(id,request_id,kind,payload,fingerprint,status,created) VALUES(?,?,?,?,?,'prepared',?)",
                       (oid, request_id, kind, rendered, fingerprint, time.time()))
            return self.decode(db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone())

    def approve(self, operation_id, expected_hash):
        with self.transaction() as db:
            row = self.decode(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
            if row["payload"]["content_hash"] != expected_hash:
                raise WriteConflict("snapshot_changed")
            if row["status"] != "prepared":
                raise WriteConflict("approval_state_conflict")
            db.execute("UPDATE operations SET status='approved',approved=? WHERE id=?", (time.time(), operation_id))

    def claim(self, operation_id):
        with self.transaction() as db:
            row = self.decode(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
            if row["status"] != "approved":
                return False
            if time.time() - row["approved"] > 600:
                raise WriteConflict("approval_expired")
            uncertain = db.execute("SELECT id FROM operations WHERE id<>? AND kind=? AND json_extract(payload,'$.content_hash')=? AND fingerprint=? AND status IN ('running','unknown')",
                                   (operation_id, row["kind"], row["payload"]["content_hash"], row["fingerprint"])).fetchone()
            if uncertain:
                raise WriteConflict("unresolved_matching_operation")
            db.execute("UPDATE operations SET status='running',started=? WHERE id=? AND status='approved'",
                       (time.time(), operation_id))
            return True

    def finish(self, operation_id, status, *, result=None, error_code=None):
        with self.transaction() as db:
            db.execute("UPDATE operations SET status=?,result=?,error_code=? WHERE id=? AND status='running'",
                       (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error_code, operation_id))

    def reconcile(self, operation_id, result):
        with self.transaction() as db:
            row = self.decode(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
            # 执行中进程可能仍存活；保留足够长的等待窗口，不夺取其结果。
            if row["status"] == "running" and time.time() - row["started"] < 180:
                raise WriteConflict("operation_running")
            if row["status"] not in {"unknown", "running"}:
                raise WriteConflict("reconciliation_state_conflict")
            confirmed = result["status"] == "found"
            db.execute("UPDATE operations SET status=?,result=?,error_code=? WHERE id=?",
                       ("confirmed" if confirmed else "unknown", json.dumps(result, ensure_ascii=False),
                        None if confirmed else "result_unconfirmed", operation_id))

    @staticmethod
    def public(item):
        return {key: item[key] for key in ("id", "kind", "status", "created", "approved", "started", "payload", "result", "error_code")}
