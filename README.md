# wewrite-platform

为 wewrite 开发的本地可视化平台（工作台）。单机、单用户、loopback 监听，把热榜、选题、写作、预览、配图和人工确认写入微信草稿箱串成一条可恢复流水线。

设计基线见 Multica 任务 JEE-1887 的《WeWrite 工作台：先看选题，再看可信产物》技术方案 v2，以及仓库内 `docs/s2-technical-design.md`（技术详细设计终版，编码基线）。

## 当前里程碑（M2）

已交付：M1（仓库骨架 + P1/P2）+ M2（P3 产物预览 + P4 每日热榜与每日快照）。

- M1：`GET /healthz`、`GET /api/v1/bootstrap`、`GET /api/v1/runs`、`GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/artifact/{name}`。
- P3 预览：`GET /api/v1/themes`（名称来自 `wewrite.toolkit.theme.list_themes()`，不接受外部路径）；`POST /api/v1/preview`（`{run_id,source:draft|article|illustrated_article,theme}`）返回 `{html,paste_safe_html,title,digest,source_sha256,filtered_count}`；复用 M1 的 `safe_open_run_file` 读白名单产物；HTML 已过滤 `<script>`/事件属性/`javascript:` 并记录 `filtered_count`；iframe `sandbox` 且不含 `allow-scripts`。
- P4 热榜：`GET /api/v1/hotspots?limit=20`（返回已保存快照）、`GET /api/v1/hotspots?limit=20&refresh=1`（只抓当下、不落盘、不更新 `last_success_at`；强制同源 `Origin`+`Sec-Fetch-Site: same-origin`，缺失/`cross-site`/`same-site` 在调用 fetcher 前返回 `403 CSRF_INVALID` + `details.reason=refresh_requires_same_origin`）、`POST /api/v1/hotspots/refresh`（`{date?,limit?}` 落盘，`date` 只命名文件）、`GET /api/v1/hotspot-snapshots`（最近 30 天日期列表）、`GET /api/v1/hotspots/{date}`（单日快照，缺失/超保留期 404）。快照落盘 `$WEWRITE_HOME/hotspots/YYYY-MM-DD.json`（含 UTC 与 Asia/Shanghai 双时间）；来源中英分离（英文 source ID 为机器键、中文 `source_label` 展示）；全源失败即使退出码 0 也判失败、不覆盖上一份成功快照。
- 中间件基线：Host 只允许 `127.0.0.1` / `[::1]` / `localhost`（不放行整个 127/8）；POST/PUT/DELETE 强制 `Content-Type: application/json`、同源 `Origin`（host 与端口一致）、`Sec-Fetch-Site: same-origin`、`X-CSRF-Token`。无 CORS。`refresh=1` 是唯一需要同源校验的 GET。
- 路径安全：进程级钉住 `$WEWRITE_HOME` 的 `st_dev/st_ino`（首次成功打开时固定，之后每次读取重开 fd 比对，不一致 `403 PATH_FORBIDDEN`）；读取时逐级 `openat`（`O_DIRECTORY|O_NOFOLLOW`）打开 `runs`、`<run_id>`，任一级 symlink/非目录统一 `403 PATH_FORBIDDEN`；产物白名单 + `O_NOFOLLOW|O_RDONLY` + `fstat(S_ISREG, st_nlink>=1)`，全程从已打开 fd 读取。
- 全局锁与原子写：`$WEWRITE_HOME/platform.lock`（`fcntl.flock`），临时文件同目录 → `fsync` → `os.replace` → 父目录 `fsync`。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
# 本机 pypi.org 不稳定时用阿里镜像（不改变依赖声明）
pip install -e . -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

wewrite-platform serve --port 8765
```

浏览器打开 `http://127.0.0.1:8765`（Host 必须是 `127.0.0.1:8765` / `localhost:8765` / `[::1]:8765`，机器 IP 或其它域名会被 403 拒绝）。

状态根默认 `~/.wewrite`，可用 `WEWRITE_HOME` 覆盖。

## 测试

```bash
pip install -e ".[test]" -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
python -m pytest -q
```

fixture 在 `tests/conftest.py`，含 3 种 run 状态（active / failed / completed）。

## 契约基线

`docs/s2-technical-design.md`（commit `bf84b078`）是编码基线，本任务描述与该设计冲突时以设计第 2/3 节为准。
