"""API 路由：bootstrap / healthz / runs（M1）+ themes / preview / hotspots（M2）。"""

from __future__ import annotations

import hashlib
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import __version__
from app import auth
from app import middleware as mw
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


# ---------- S8-c：登录鉴权 ----------

def _login_redirect_back(request: Request) -> RedirectResponse:
    """登录成功后回到来源页（默认首页），只接受站内相对路径，拦截越权跳转。

    清洗规则与登录页前端、``app.middleware.safe_next`` 共用同一口径（见该函数）。
    """
    nxt = mw.safe_next(request.query_params.get("next", ""), default="/")
    return RedirectResponse(url=nxt, status_code=303)


@router.post("/auth/login")
async def login(request: Request):
    """账号密码登录：校验通过发会话 cookie（HttpOnly + SameSite）。

    请求为表单（``application/x-www-form-urlencoded``），不要求 CSRF（登录本身无会话）。
    """
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    if not username or not password or not request.app.state.users.verify(username, password):
        return JSONResponse(status_code=401, content={"code": "AUTH_INVALID", "message": "账号或密码错误"})

    token = request.app.state.sessions.create()
    bare_host, _port = mw.normalize_host_port(request.headers.get("host"))
    secure = mw.effective_scheme(request.scope, request.headers) == "https" and not mw.is_loopback_bare(bare_host)
    response = JSONResponse(status_code=200, content={"ok": True, "username": username})
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request):
    """登出：撤销当前会话并清除 cookie。"""
    token = request.cookies.get(auth.SESSION_COOKIE)
    request.app.state.sessions.revoke(token)
    response = JSONResponse(status_code=200, content={"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


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


_IMAGE_FILENAME_RE = re.compile(r"^(?:cover|\d{1,3})\.png$")


@router.get("/api/v1/runs/{run_id}/images/{filename}")
async def get_run_image(run_id: str, filename: str):
    """读取 run 目录下 images/ 的封面或序号图（P6 逐图缩略图用）。

    只允许 ``images/cover.png`` 或 ``images/<n>.png``（n 为 0–999），复用
    ``safe_read_run_relative`` 的逐级 openat + O_NOFOLLOW 防线，缺失返回 404。
    """
    from fastapi.responses import Response

    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    if not isinstance(filename, str) or not _IMAGE_FILENAME_RE.fullmatch(filename):
        return _error_response(errors.validation_error("图片文件名不合法", {"field": "filename"}))
    rel = f"images/{filename}"
    try:
        data = paths.safe_read_run_relative(_home(), run_id, rel)
    except errors.ApiError as exc:
        return _error_response(exc)
    if data is None:
        return _error_response(errors.not_found({"reason": "image_not_found"}))
    return Response(content=data, media_type="image/png")


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


@router.get("/api/v1/image-providers")
async def list_image_providers():
    """只读端点：内置图片供应商元数据，供 P8 配置页渲染 provider 下拉与联动。

    只做 Host 校验（与 /themes 一致），不需要 CSRF；数据来自
    ``wewrite.toolkit.image_gen.PROVIDERS`` 的动态反射，平台侧不硬编码。
    """
    from app.adapters import image_providers

    return {
        "providers": image_providers.list_image_providers(),
        "supports_custom": True,
    }


@router.get("/api/v1/writer-providers")
async def list_writer_providers():
    """只读端点：内置写作模型供应商元数据，供 P8「写作模型」区块渲染 provider 下拉。

    ``custom`` 作为「自定义…」语义由前端处理；数据来自
    ``app.adapters.writer_providers``，集中一处便于加厂商。
    """
    from app.adapters import writer_providers

    return {
        "providers": writer_providers.list_writer_providers(),
        "supports_custom": True,
    }


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

    # 公网 HTTPS 域名与回环 http 的 scheme 语义不同，origin/referer 校验需按请求
    # 实际 scheme 计算默认端口（https→443，http→80）。
    scheme = mw.effective_scheme(request.scope, request.headers)

    if origin:
        return origin_host_matches(origin, bare_host, host_port, scheme)
    if referer:
        return referer_host_matches(referer, bare_host, host_port, scheme)
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


@router.get("/api/v1/jobs")
async def list_jobs(run_id: str | None = None):
    """按 run_id 过滤列出 job（供 P2 详情页定位重试/进度入口）。"""
    try:
        store = jobs.JobStore(_home())
        all_jobs = store.list_jobs()
    except errors.ApiError as exc:
        return _error_response(exc)
    if run_id is not None:
        try:
            paths.validate_run_id(run_id)
        except errors.ApiError as exc:
            return _error_response(exc)
        all_jobs = [j for j in all_jobs if j.get("run_id") == run_id]
    return {"jobs": all_jobs}


@router.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    """job 全量投影（M3 只读 queued/cancelled，供 P5 确认后跳转）。"""
    try:
        job = jobs.JobStore(_home()).get_or_404(job_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    return job


# ---------- M4：P8 配置 / P7 发布 / P6 job ----------

async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise errors.validation_error("请求体必须为合法 JSON")
    if not isinstance(body, dict):
        raise errors.validation_error("请求体必须为 JSON 对象")
    return body


def _config_store() -> "config_store.ConfigStore":
    from app.storage import config_store

    return config_store.ConfigStore(_home())


@router.get("/api/v1/config")
async def get_config():
    try:
        return _config_store().project_config()
    except errors.ApiError as exc:
        return _error_response(exc)


@router.put("/api/v1/config")
async def put_config(request: Request):
    body = await _read_body(request)
    try:
        return _config_store().put_config(body)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.get("/api/v1/style")
async def get_style():
    try:
        return _config_store().project_style()
    except errors.ApiError as exc:
        return _error_response(exc)


@router.put("/api/v1/style")
async def put_style(request: Request):
    body = await _read_body(request)
    try:
        return _config_store().put_style(body)
    except errors.ApiError as exc:
        return _error_response(exc)


def _style_generate_backend(request: Request):
    from app.adapters import writer as writer_adapter

    return getattr(request.app.state, "style_generate_backend", None) or writer_adapter.StyleGenerateBackend()


@router.post("/api/v1/style/validate")
async def validate_style(request: Request):
    """校验 style.yaml 文本：返回语法/字段问题列表（行号 + 中文原因），不写盘。"""
    body = await _read_body(request)
    try:
        return _config_store().validate_style_yaml(body.get("yaml", ""))
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/style/generate")
async def generate_style(request: Request):
    """智能生成整份 style.yaml（可选 AI 引导入口）。

    出网调用写作模型，绝不落盘，只回显经过白名单裁剪、重新序列化的 YAML 全文；
    未配置写作模型返回 503 WRITER_NOT_CONFIGURED，调用失败返回 502 UPSTREAM_ERROR。
    """
    body = await _read_body(request)
    try:
        return await run_in_threadpool(
            _config_store().generate_style_yaml, body, _style_generate_backend(request)
        )
    except errors.ApiError as exc:
        return _error_response(exc)


@router.get("/api/v1/runs/{run_id}/publish/preflight")
async def publish_preflight(run_id: str):
    from app.domain import preflight as preflight_mod

    try:
        paths.validate_run_id(run_id)
        return preflight_mod.check(_home(), run_id)
    except errors.ApiError as exc:
        return _error_response(exc)


def _publish_gate(request: Request):
    from app.adapters import wechat as wechat_adapter
    from app.domain import publish_gate

    adapter = getattr(request.app.state, "wechat_adapter", None) or wechat_adapter.WeChatAdapter()
    return publish_gate.PublishGate(_home(), adapter=adapter)


@router.put("/api/v1/runs/{run_id}/publish/permission")
async def publish_permission(run_id: str, request: Request):
    body = await _read_body(request)
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    if not isinstance(body.get("allow"), bool):
        return _error_response(errors.validation_error("allow 必须为布尔", {"field": "allow"}))
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return _error_response(errors.validation_error("reason 必须为字符串", {"field": "reason"}))
    try:
        return await run_in_threadpool(
            _publish_gate(request).set_permission, run_id, body["allow"], reason
        )
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/runs/{run_id}/publish/token")
async def publish_token(run_id: str, request: Request):
    body = await _read_body(request)
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    revision = body.get("revision")
    title = body.get("title")
    if not isinstance(revision, str) or not revision:
        return _error_response(errors.validation_error("revision 必填", {"field": "revision"}))
    if not isinstance(title, str) or not title:
        return _error_response(errors.validation_error("title 必填", {"field": "title"}))
    if body.get("checked") is not True:
        return _error_response(errors.validation_error("checked 必须为 true", {"field": "checked"}))
    try:
        return await run_in_threadpool(_publish_gate(request).issue_token, run_id, revision, title)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/runs/{run_id}/publish")
async def publish(run_id: str, request: Request):
    body = await _read_body(request)
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    token = body.get("token")
    title = body.get("title")
    if not isinstance(token, str) or not token:
        return _error_response(errors.validation_error("token 必填", {"field": "token"}))
    if not isinstance(title, str) or not title:
        return _error_response(errors.validation_error("title 必填", {"field": "title"}))
    if body.get("checked") is not True:
        return _error_response(errors.validation_error("checked 必须为 true", {"field": "checked"}))
    try:
        return await run_in_threadpool(_publish_gate(request).commit_once, run_id, token, title)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/runs/{run_id}/publish/reconcile")
async def publish_reconcile(run_id: str, request: Request):
    body = await _read_body(request)
    try:
        paths.validate_run_id(run_id)
    except errors.ApiError as exc:
        return _error_response(exc)
    media_id = body.get("media_id")
    if media_id is not None and not isinstance(media_id, str):
        return _error_response(errors.validation_error("media_id 必须为字符串", {"field": "media_id"}))
    try:
        return await run_in_threadpool(_publish_gate(request).reconcile, run_id, media_id)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    try:
        return jobs.JobStore(_home()).cancel(job_id)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    try:
        return await run_in_threadpool(jobs.JobStore(_home()).resume, job_id)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/jobs/{job_id}/regenerate")
async def regenerate_job(job_id: str, request: Request):
    """JEE-1963：「成功但字数不足」任务重新生成（产生新 job，不清旧成功记录）。

    准入：旧 job ``succeeded`` 且其 run 带 ``length_warning``；并发保护下连点两次
    只产生一次写作（第二次 CONFLICT）。普通 succeeded（无警告）仍不可重试。
    """
    try:
        return await run_in_threadpool(jobs.JobStore(_home()).regenerate, job_id)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/jobs/{job_id}/accept-existing")
async def accept_existing_job(job_id: str, request: Request):
    body = await _read_body(request)
    if body.get("confirm") is not True:
        return _error_response(errors.validation_error("confirm 必须为 true", {"field": "confirm"}))
    try:
        return jobs.JobStore(_home()).accept_existing(job_id)
    except errors.ApiError as exc:
        return _error_response(exc)


@router.post("/api/v1/jobs/{job_id}/images/retry")
async def retry_job_images(job_id: str, request: Request):
    body = await _read_body(request)
    indices = body.get("indices")
    if not isinstance(indices, list):
        return _error_response(errors.validation_error("indices 必须为数组", {"field": "indices"}))
    try:
        return jobs.JobStore(_home()).retry_images(job_id, indices)
    except errors.ApiError as exc:
        return _error_response(exc)


def datetime_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
