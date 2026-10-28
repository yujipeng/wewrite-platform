"""FastAPI 应用工厂与静态资源挂载。

静态资源与模板随包发布（package-data 配好），``pip install`` 后即可用。
静态根为 ``app/web``。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app import auth
from app.api.routes import router
from app.domain import errors
from app.middleware import CsrfStore, HostCsrfMiddleware, SessionStore

_WEB_DIR = Path(__file__).resolve().parent / "web"

# 进程内 worker 轮询间隔（秒）
_WORKER_POLL_SECONDS = 1.0


def _web_dir() -> Path:
    """优先用包内 web 目录，兼容开发态（仓库根/web）。"""
    if _WEB_DIR.is_dir():
        return _WEB_DIR
    return Path(__file__).resolve().parent.parent / "web"


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动进程内 asyncio worker：串行消费 queued job（P6）。

    测试可设 ``WEWRITE_PLATFORM_NO_WORKER=1`` 或 ``app.state.start_worker=False``
    关闭自动消费，改为显式调用 ``Worker.run_once()``。
    """
    if getattr(app.state, "start_worker", True) and os.environ.get("WEWRITE_PLATFORM_NO_WORKER") != "1":
        from app.worker.executor import Worker

        home = os.environ.get("WEWRITE_HOME", "") or os.path.join(os.path.expanduser("~"), ".wewrite")
        worker = Worker(home)
        app.state.worker = worker

        async def _loop():
            while True:
                try:
                    worker.run_once()
                    worker.recover_stale()
                except Exception:
                    # 单次消费异常不终止调度器；诊断留在 job.last_error / 日志
                    pass
                await asyncio.sleep(_WORKER_POLL_SECONDS)

        task = asyncio.create_task(_loop())
        app.state.worker_task = task
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    else:
        yield


def create_app() -> FastAPI:
    csrf = CsrfStore()
    sessions = SessionStore()
    try:
        users = auth.UserStore(os.environ.get("WEWRITE_USERS"))
    except auth.UsernameInvalid:
        # 结构非法（空账号/缺冒号/重复账号）时 fail closed：无任何可用账号，
        # 登录一律失败，不阻断启动。
        users = auth.UserStore(None)
        users.rejected = []
    # 非 bcrypt 哈希的条目被静默拒绝（fail closed），这里仅记录不含密码内容的账号名，
    # 便于定位配置问题；绝不打印 secret。
    if users.rejected:
        import logging

        logging.getLogger("wewrite").warning(
            "WEWRITE_USERS 中以下账号的口令不是受支持的 bcrypt 哈希，已拒绝该账号：%s",
            ",".join(users.rejected),
        )

    app = FastAPI(
        title="wewrite-platform",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    app.state.csrf = csrf
    app.state.sessions = sessions
    app.state.users = users
    app.state.server_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    app.state.start_worker = True

    @app.exception_handler(errors.ApiError)
    async def api_error_handler(request: Request, exc: errors.ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    app.include_router(router)

    web_dir = _web_dir()

    # 静态资源：css/js 等
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    # 登录页（公网域名未登录时的落地页）
    @app.get("/login")
    async def login_page(request: Request):
        return FileResponse(str(web_dir / "login.html"))

    # 登出页：先由 POST /auth/logout 撤销会话，再回到登录页
    @app.get("/logout")
    async def logout_page(request: Request):
        return FileResponse(str(web_dir / "logout.html"))

    @app.get("/")
    async def index():
        return FileResponse(str(web_dir / "index.html"))

    # 页面路由（P1 首页 / P2 详情）：同一 index.html 前端按 hash 路由
    @app.get("/runs/{run_id}")
    async def run_page(run_id: str):
        return FileResponse(str(web_dir / "index.html"))

    # 挂载中间件：Host 校验 + 登录鉴权 + 变更方法 CSRF/Content-Type 校验
    app.add_middleware(HostCsrfMiddleware, csrf=csrf, sessions=sessions, users=users)

    return app


app = create_app()
