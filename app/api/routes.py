"""API 路由：bootstrap / healthz / runs（M1）+ themes / preview / hotspots（M2）。"""

from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.adapters import hotspots as hotspots_adapter
from app.adapters import preview as preview_adapter
from app.domain import errors
from app.middleware import normalize_host_port, origin_host_matches
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
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
    return {"name": name, "sha256": digest, "content": text}


# ---------- M2：P3 预览 + P4 热榜 ----------

def _fetcher(request: Request) -> hotspots_adapter.HotspotFetcher:
    """取进程内热榜 fetcher（测试可注入 fake 以计数零出网）。"""
    return getattr(request.app.state, "hotspot_fetcher", None) or hotspots_adapter.HotspotFetcher()


@router.get("/api/v1/themes")
async def list_themes():
    from wewrite.toolkit.theme import list_themes as _list, load_theme

    names = _list()
    themes = []
    for name in names:
        try:
            t = load_theme(name)
            description = getattr(t, "description", "")
        except Exception:
            description = ""
        themes.append({"name": name, "description": description})
    return {"themes": themes}


@router.post("/api/v1/preview")
async def preview(request: Request):
    """P3 产物预览：正文来源切换 + 主题 + 转换 + 过滤 + paste_safe。"""
    try:
        body = await request.json()
    except Exception:
        return _error_response(errors.validation_error("请求体必须为合法 JSON"))
    if not isinstance(body, dict):
        return _error_response(errors.validation_error("请求体必须为 JSON 对象"))

    run_id = body.get("run_id")
    source = body.get("source")
    theme = body.get("theme")

    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)

    try:
        result = preview_adapter.preview(_home(), run_id, source, theme)
    except errors.ApiError as exc:
        return _error_response(exc)
    return result


def _require_same_origin_refresh(request: Request) -> bool:
    """refresh=1 的同源校验（设计 :87/:98）：Origin 必须是三种回环 host 之一且端口一致，
    Sec-Fetch-Site 必须 same-origin。返回 False 表示应拒绝（不得发起抓取）。"""
    host_header = request.headers.get("host")
    bare_host, host_port = normalize_host_port(host_header)
    origin = request.headers.get("origin")
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site != "same-origin":
        return False
    if not origin_host_matches(origin, bare_host, host_port):
        return False
    return True


@router.get("/api/v1/hotspots")
async def get_hotspots(request: Request):
    """P4 热榜：refresh=1 只抓取当下数据（不落盘）；否则返回已保存快照。"""
    limit_raw = request.query_params.get("limit", "20")
    refresh_raw = request.query_params.get("refresh", "0")

    try:
        limit = hotspots_adapter.normalize_limit(limit_raw)
    except errors.ApiError as exc:
        return _error_response(exc)

    if str(refresh_raw) == "1":
        # 必须先做同源校验，校验失败不得调用 fetcher（零出网）
        if not _require_same_origin_refresh(request):
            return _error_response(
                errors.csrf_invalid({"reason": "refresh_requires_same_origin"})
            )
        try:
            data = hotspots_adapter.HotspotStore(_home()).fetch_now(_fetcher(request), limit)
        except errors.ApiError as exc:
            return _error_response(exc)
        return data

    try:
        data = hotspots_adapter.HotspotStore(_home()).read_latest()
    except errors.ApiError as exc:
        return _error_response(exc)
    return data


@router.get("/api/v1/hotspot-snapshots")
async def list_hotspot_snapshots():
    try:
        dates = hotspots_adapter.HotspotStore(_home()).list_dates()
    except errors.ApiError as exc:
        return _error_response(exc)
    return {"dates": dates}


@router.get("/api/v1/hotspots/{date}")
async def get_hotspot_snapshot(date: str):
    try:
        snap = hotspots_adapter.HotspotStore(_home()).read_date(date)
    except errors.ApiError as exc:
        return _error_response(exc)
    return snap


@router.post("/api/v1/hotspots/refresh")
async def refresh_hotspots(request: Request):
    """P4 落盘刷新：date 只命名文件，抓取的是当下数据；全失败不覆盖。"""
    try:
        body = await request.json()
    except Exception:
        return _error_response(errors.validation_error("请求体必须为合法 JSON"))
    if not isinstance(body, dict):
        return _error_response(errors.validation_error("请求体必须为 JSON 对象"))

    date = body.get("date")
    limit_raw = body.get("limit", 20)
    try:
        limit = hotspots_adapter.normalize_limit(limit_raw)
    except errors.ApiError as exc:
        return _error_response(exc)
    if date is not None:
        try:
            hotspots_adapter.validate_date(date)
        except errors.ApiError as exc:
            return _error_response(exc)

    try:
        data = hotspots_adapter.HotspotStore(_home()).fetch_and_persist(
            _fetcher(request), limit, date
        )
    except errors.ApiError as exc:
        return _error_response(exc)
    return data
