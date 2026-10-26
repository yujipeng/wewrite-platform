"""FastAPI 应用工厂与静态资源挂载。

静态资源与模板随包发布（package-data 配好），``pip install`` 后即可用。
静态根为 ``app/web``。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.domain import errors
from app.middleware import CsrfStore, HostCsrfMiddleware

_WEB_DIR = Path(__file__).resolve().parent / "web"


def _web_dir() -> Path:
    """优先用包内 web 目录，兼容开发态（仓库根/web）。"""
    if _WEB_DIR.is_dir():
        return _WEB_DIR
    return Path(__file__).resolve().parent.parent / "web"


def create_app() -> FastAPI:
    csrf = CsrfStore()
    app = FastAPI(
        title="wewrite-platform",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.csrf = csrf
    app.state.server_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @app.exception_handler(errors.ApiError)
    async def api_error_handler(request: Request, exc: errors.ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    app.include_router(router)

    web_dir = _web_dir()

    # 静态资源：css/js 等
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(web_dir / "index.html"))

    # 页面路由（P1 首页 / P2 详情）：同一 index.html 前端按 hash 路由
    @app.get("/runs/{run_id}")
    async def run_page(run_id: str):
        return FileResponse(str(web_dir / "index.html"))

    # 挂载中间件：Host 校验 + 变更方法 CSRF/Content-Type 校验
    app.add_middleware(HostCsrfMiddleware, csrf=csrf)

    return app


app = create_app()
