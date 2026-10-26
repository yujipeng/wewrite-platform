"""API 路由：bootstrap / healthz / runs（M1）+ themes / preview / hotspots（M2）。"""

from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.adapters import hotspots as hotspots_adapter
from app.adapters import preview as preview_adapter
from app.domain import confirm_gate
from app.domain import errors
from app.middleware import normalize_host_port, origin_host_matches, referer_host_matches
from app.storage import candidates
from app.storage import jobs
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
    """refresh=1 的同源校验（设计 :87/:98，按项目编排返修口径修正）。

    同源 GET 不携带 ``Origin``（仅同源 POST 才带），因此主闸门是
    ``Sec-Fetch-Site: same-origin``（浏览器强制、页面脚本改不了）：

    1. ``Sec-Fetch-Site`` 必须 ``same-origin``；
    2. ``Origin`` 存在时必须同源（host+端口）；不存在时校验 ``Referer``
       的 scheme+host+端口 与 Host 一致；
    3. 两者都缺失 → 拒绝（非浏览器客户端走这条），不调用 fetcher。
    """
    host_header = request.headers.get("host")
    bare_host, host_port = normalize_host_port(host_header)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    sec_fetch_site = request.headers.get("sec-fetch-site")

    if sec_fetch_site != "same-origin":
        return False

    if origin:
        return origin_host_matches(origin, bare_host, host_port)
    if referer:
        return referer_host_matches(referer, bare_host, host_port)
    return False


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
            data = await run_in_threadpool(
                hotspots_adapter.HotspotStore(_home()).fetch_now, _fetcher(request), limit
            )
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
        data = await run_in_threadpool(
            hotspots_adapter.HotspotStore(_home()).fetch_and_persist,
            _fetcher(request),
            limit,
            date,
        )
    except errors.ApiError as exc:
        return _error_response(exc)
    return data


# ---------- M3：P5 选题确认（候选池 + 确认门） ----------

def _candidate_store() -> "candidates.CandidateStore":
    return candidates.CandidateStore(_home())


def _confirm_gate(request: Request) -> "confirm_gate.ConfirmGate":
    adapter = getattr(request.app.state, "wewrite_adapter", None)
    gate = confirm_gate.ConfirmGate(_home(), adapter=adapter)
    return gate


@router.get("/api/v1/candidates")
async def list_candidates(request: Request):
    """候选集摘要列表，可按 date / status 过滤。"""
    date = request.query_params.get("date")
    status = request.query_params.get("status")
    try:
        if date is not None:
            candidates.validate_date(date)
        if status is not None and status not in {"open", "confirmed", "superseded", "deleted", "expired"}:
            raise errors.validation_error("status 取值不合法", {"field": "status"})
    except errors.ApiError as exc:
        return _error_response(exc)
    try:
        sets = _candidate_store().list_sets(date=date, status=status)
    except errors.ApiError as exc:
        return _error_response(exc)
    return {"candidates": sets}


@router.get("/api/v1/candidates/{candidate_set_id}")
async def get_candidate_set(candidate_set_id: str):
    """返回完整候选集、过期/关闭状态与 snapshot hash。"""
    try:
        candidates.validate_candidate_set_id(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    try:
        data = _candidate_store().get_or_404(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    return _candidate_store()._summary(data, candidates._set_status(data, datetime_now_utc()))


@router.post("/api/v1/candidates")
async def generate_candidates(request: Request):
    """从指定日期快照生成候选集（10 条，全失败不写）。"""
    try:
        body = await request.json()
    except Exception:
        return _error_response(errors.validation_error("请求体必须为合法 JSON"))
    if not isinstance(body, dict):
        return _error_response(errors.validation_error("请求体必须为 JSON 对象"))

    date = body.get("date")
    style_revision = body.get("style_revision")
    force = bool(body.get("force", False))
    try:
        candidates.validate_date(date)
        from app.domain import scoring

        scoring.validate_style_revision(style_revision)
    except errors.ApiError as exc:
        return _error_response(exc)

    # 读取当日快照（缺失 404）
    try:
        snapshot = hotspots_adapter.HotspotStore(_home()).read_date(date)
    except errors.ApiError as exc:
        return _error_response(exc)

    # 全失败快照不得生成（承接 M2 加严项）
    if hotspots_adapter.is_all_failed(snapshot):
        return _error_response(
            errors.validation_error(
                "当日快照全源失败，无可用条目，请先补抓", {"reason": "all_failed"}
            )
        )

    try:
        result = await run_in_threadpool(
            _candidate_store().generate, date, style_revision, snapshot, force
        )
    except errors.ApiError as exc:
        return _error_response(exc)
    candidate_set, created = result
    return _candidate_store()._summary(candidate_set, candidates._set_status(candidate_set, datetime_now_utc()))


@router.delete("/api/v1/candidates/{candidate_set_id}")
async def delete_candidate_set(candidate_set_id: str):
    """删除候选集（仅从未确认、无关联的 open 集可置 deleted 墓碑）。"""
    try:
        candidates.validate_candidate_set_id(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    try:
        _candidate_store().delete(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    return {"candidate_set_id": candidate_set_id, "set_status": "deleted"}


@router.post("/api/v1/candidates/{candidate_set_id}/confirm")
async def confirm_candidate(candidate_set_id: str, request: Request):
    """确认门：校验 + intent journal + 上游 create_run + queued job。"""
    try:
        candidates.validate_candidate_set_id(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    idempotency_key = request.headers.get("idempotency-key") or request.headers.get("Idempotency-Key")
    try:
        body = await request.json()
    except Exception:
        return _error_response(errors.validation_error("请求体必须为合法 JSON"))
    try:
        settings = confirm_gate._validate_confirm_body(body)
    except errors.ApiError as exc:
        return _error_response(exc)

    gate = _confirm_gate(request)
    try:
        result = await run_in_threadpool(
            gate.confirm, candidate_set_id, settings, idempotency_key
        )
    except errors.ApiError as exc:
        return _error_response(exc)
    return result


@router.post("/api/v1/candidates/{candidate_set_id}/retract")
async def retract_candidate(candidate_set_id: str, request: Request):
    """撤回候选：仅 job 仍 queued 时可撤回。"""
    try:
        candidates.validate_candidate_set_id(candidate_set_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    try:
        body = await request.json()
    except Exception:
        return _error_response(errors.validation_error("请求体必须为合法 JSON"))
    if not isinstance(body, dict):
        return _error_response(errors.validation_error("请求体必须为 JSON 对象"))
    candidate_id = body.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return _error_response(errors.validation_error("candidate_id 必填", {"field": "candidate_id"}))

    gate = _confirm_gate(request)
    try:
        result = await run_in_threadpool(gate.retract, candidate_set_id, candidate_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    return result


@router.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    """job 全量投影（M3 只读 queued/cancelled，供 P5 确认后跳转）。"""
    try:
        job = jobs.JobStore(_home()).get_or_404(job_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    return job


def datetime_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
