"""命令行入口：``wewrite-platform serve`` 一条命令起服务。"""

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

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from app.main import app

        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
