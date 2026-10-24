"""启动 stdio MCP 或在人工终端批准一个不可变快照。"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .server import create_server
from .write_store import WriteConflict, WriteStore


def main():
    parser = argparse.ArgumentParser(description="三平台 MCP，本地 stdio 服务")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--enable-writes", action="store_true", help="注册微博写操作，仍须终端批准")
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.add_argument("action", nargs="?", choices=["serve", "approve"], default="serve")
    parser.add_argument("operation_id", nargs="?")
    args = parser.parse_args()
    settings = Settings.from_environment()
    if args.state_dir is not None:
        from dataclasses import replace
        settings = replace(settings, state_dir=args.state_dir.expanduser())
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if args.action == "approve":
        if not args.operation_id or not sys.stdin.isatty():
            parser.error("批准需要用户在交互终端运行并提供 operation_id。")
        store = WriteStore(settings.state_dir)
        try:
            item = store.get(args.operation_id)
            print(json.dumps(store.public(item), ensure_ascii=False, indent=2))
            expected = f"批准 {item['id']}"
            entered = input(f"确认上面的账号、正文、目标及公开/AI 声明后，输入「{expected}」：")
            if entered != expected:
                parser.error("未批准，操作未发送。")
            store.approve(item["id"], item["payload"]["content_hash"])
            print("已批准；MCP 可执行这个快照，批准在 10 分钟后过期。")
        except WriteConflict as exc:
            parser.error(str(exc))
    else:
        create_server(settings, writes_enabled=args.enable_writes).run(transport="stdio")


if __name__ == "__main__":
    main()
