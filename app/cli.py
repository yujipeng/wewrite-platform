"""命令行入口：``wewrite-platform serve`` 起服务 + ``hotspots refresh`` 补抓。"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wewrite-platform", description="WeWrite 本地可视化工作台")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地服务")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")

    hotspots = sub.add_parser("hotspots", help="热榜补抓")
    hotspots_sub = hotspots.add_subparsers(dest="hotspots_command")
    refresh = hotspots_sub.add_parser("refresh", help="立即补抓并落盘热榜快照")
    refresh.add_argument("--limit", type=int, default=20, help="抓取条数（1–100）")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from app.main import app

        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
        return 0

    if args.command == "hotspots" and args.hotspots_command == "refresh":
        from app.adapters.hotspots import HotspotFetcher, HotspotStore

        home = os.environ.get("WEWRITE_HOME", "") or os.path.join(os.path.expanduser("~"), ".wewrite")
        try:
            data = HotspotStore(home).fetch_and_persist(HotspotFetcher(), args.limit)
        except Exception as exc:  # noqa: BLE001
            print(f"补抓失败：{type(exc).__name__}", file=sys.stderr)
            return 1
        print(f"已补抓 {data.get('count', 0)} 条；来源={data.get('sources')} 失败源={data.get('sources_failed')}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
