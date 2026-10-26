"""API 路由：bootstrap / healthz / runs（M1 只读核心）。"""

from __future__ import annotations

import hashlib
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.domain import errors
from app.storage import paths, projections

router = APIRouter()


def _home() -> str:
    return os.environ.get("WEWRITE_HOME", "") or os.path.join(os.path.expanduser("~"), ".wewrite")


def _error_response(exc: errors.ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@router.get("/api/v1/bootstrap")
async def bootstrap(request: Request):
    app_state = request.app.state
    csrf: object = app_state.csrf
    token = csrf.issue()
    return {
        "csrf_token": token,
        "version": __version__,
        "server_started_at": app_state.server_started_at,
    }


@router.get("/healthz")
async def healthz():
    return {"status": "ok", "version": __version__}


@router.get("/api/v1/runs")
async def list_runs(request: Request):
    home = _home()
    try:
        runs = projections.list_runs(home)
    except errors.ApiError as exc:
        return _error_response(exc)
    return {
        "runs": [
            {
                "run_id": r.run_id,
                "title": r.title,
                "status": r.status,
                "mode": r.mode,
                "artifacts": r.artifacts,
                "publish": r.publish,
                "created": r.created,
                "updated": r.updated,
            }
            for r in runs
        ]
    }


@router.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    try:
        detail = projections.get_run_detail(_home(), run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    if detail is None:
        return _error_response(errors.not_found())
    return detail


@router.get("/api/v1/runs/{run_id}/artifact/{name}")
async def get_artifact(run_id: str, name: str, request: Request):
    """安全读取单个产物，返回 {name,sha256,content}；与 POST preview 共用 safe_open_run_file。"""
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    if name not in paths._ARTIFACT_FILES:
        return _error_response(errors.not_found({"reason": "artifact_not_whitelisted"}))
    try:
        fd, _st = paths.safe_open_run_file(_home(), run_id, name)
    except errors.ApiError as exc:
        return _error_response(exc)
    with os.fdopen(fd, "rb") as f:
        content = f.read()
    digest = hashlib.sha256(content).hexdigest()
    # 文本产物以 UTF-8 解码返回字符串，图片等二进制在 M2 预览阶段再扩展
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
    return {"name": name, "sha256": digest, "content": text}
