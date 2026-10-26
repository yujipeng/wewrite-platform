# wewrite-platform

为 wewrite 开发的本地可视化平台（工作台）。单机、单用户、loopback 监听，把热榜、选题、写作、预览、配图和人工确认写入微信草稿箱串成一条可恢复流水线。

设计基线见 Multica 任务 JEE-1887 的《WeWrite 工作台：先看选题，再看可信产物》技术方案 v2，以及仓库内 `docs/s2-technical-design.md`（技术详细设计终版，编码基线）。

## 当前里程碑（M1）

已交付：仓库骨架 + P1 工作台首页（run 列表）+ P2 任务详情（只读核心）。

- `GET /healthz` 返回 `{status:"ok",version}`（只做 Host 校验，M4 的 compose healthcheck 依赖它）。
- `GET /api/v1/bootstrap` 返回 `{csrf_token,version,server_started_at}`（token 只在进程内存，重启失效）。
- `GET /api/v1/runs`、`GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/artifact/{name}`：平台自己的只读投影，不调用上游 `wewrite.runs.load_run`（避免 GET 副作用）。`GET /api/v1/runs` 的可选 `query` 参数在 M1 由前端过滤实现（题名/run_id），后续里程碑按需补服务端过滤。
- 中间件基线：Host 只允许 `127.0.0.1` / `[::1]` / `localhost`（不放行整个 127/8）；POST/PUT/DELETE 强制 `Content-Type: application/json`、同源 `Origin`（host 与端口一致）、`Sec-Fetch-Site: same-origin`、`X-CSRF-Token`。无 CORS。
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
