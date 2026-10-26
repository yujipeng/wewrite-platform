# WeWrite 工作台 S2 技术详细设计

本文是 JEE-1908 的编码基线。目标是单机、单用户、loopback 上把热榜、选题、写作、预览、配图和人工确认写入微信草稿箱串成一条可恢复流水线。数据库、账号体系、自动发布和复制 `wewrite` 实现均不在范围内。

## 1. 选型与约束

- 后端固定 `FastAPI` + `uvicorn`，单进程、`workers=1`；长任务由进程内 asyncio 调度器串行消费一个 durable job 队列，外部慢调用使用受控线程/子进程，重启后由 job 文件恢复。
- 前端固定原生 HTML/CSS/ES modules，无 Node/Vite 构建；FastAPI 同进程挂载 `web/dist` 静态文件。这样 M1 在只有 Python 的空仓即可运行，减少前端依赖和跨源问题。
- Python 依赖 `wewrite==4.2.1`（Python >=3.11）。平台只调用其公开函数/CLI：`runs`、`WeChatConverter`/`make_paste_safe`、`load_theme`、`generate_batch`、`upload_image`/`upload_thumb`、`create_draft`/`get_draft`、`llm-write`。不得复制 converter/theme/publisher/image_gen/runs；需要能力时提上游 PR。
- 状态源为 `$WEWRITE_HOME` 下 YAML/JSON/Markdown/图片文件；不引入 SQLite、Redis、Celery。所有写入由 `platform.lock`（`flock`）保护，临时文件同目录写入、`fsync` 后 `os.replace`，目录也 `fsync`。
- 单机前提是平台是该 `WEWRITE_HOME` 的唯一写者；外部 `wewrite` CLI 只读或先停止平台。上游 `_atomic_write_text` 自身没有锁，平台不能宣称能约束外部进程。
- 监听默认 `127.0.0.1:8765`。Docker 进程监听容器 `0.0.0.0`，compose 只映射宿主 `127.0.0.1:8765`。

### 上游真实接口差异

`validate_run_id` 是 `^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$`；`load_run` 会升级并回写旧状态，HTTP GET 只能用平台自己的 `safe_load_run`。`create_run` 默认 `allow_publish=false`，平台创建时总是显式传入 `run_id` 关联和权限。`generate_batch` 只返回整批结果且无回调；单图函数内部每 provider 最多重试两次并 fallback，平台不再叠加重试，若严格零重试的真实 provider 未得到上游配置能力则 fail closed（`IMAGE_RETRY_POLICY_UNSUPPORTED`）。`llm-write` 是可选文件到文件适配器，未配置时返回退出码 3，平台将 job 标为可解释失败而不伪造正文。

## 2. `WEWRITE_HOME` 布局与并发

```
$WEWRITE_HOME/
  config.yaml                 # 0600，平台配置投影
  style.yaml                  # 0600
  platform.lock               # 全局写锁
  current_run                 # 上游兼容指针，非业务真相
  runs/<run_id>/{state.yaml,sources.yaml,article.md,draft.md,article-illustrated.md,
                 brief.yaml,review-report.json,images.json,images/,preview.html}
  hotspots/YYYY-MM-DD.json    # 所有引用均相对 WEWRITE_HOME
  candidates/YYYY-MM-DD-<hash>.json
  jobs/<job_id>.json
  publish/<token_id>.json
  audit/YYYY-MM-DD.jsonl
```

读路径必须先校验 `run_id`，再把产物名映射到固定白名单：`brief,article,draft,illustrated_article,review_report,images_manifest,sources,preview`。拒绝绝对路径、`..`、符号链接（`lstat` 后 `realpath` 必须仍在 `WEWRITE_HOME`），禁止把任意文件名交给接口。

每次 JSON/YAML 写入执行：取得全局锁 → 读取并校验版本 → 同目录临时文件 `0600` → UTF-8 写入、`fsync` → `os.replace` → 父目录 `fsync` → 释放锁。单文件锁不能替代全局锁。崩溃恢复扫描 `jobs/*.json`：`running` 无心跳超过 120 秒转 `unknown`（外部调用可能已发生），`queued` 可撤回；绝不根据未知状态重放微信写入。

### 2.1 候选集 `candidates/<date>-<hash>.json`

```json
{"version":1,"candidate_set_id":"20261026-a1b2c3","created_at":"2026-10-26T08:00:00Z","expires_at":"2026-10-27T08:00:00Z","timezone":"Asia/Shanghai","hotspot_snapshot":"hotspots/2026-10-26.json","snapshot_sha256":"...","style_revision":"sha256:...","items":[{"id":"c01","status":"proposed","title":"标题（20-28字）","angle":"切入角度","scores":{"hot":92.1,"hot_normalized":100.0,"relevance":82.0,"value":76.0,"total":84.63},"framework":"问题-证据-行动","source":"weibo","source_url":"https://example.invalid/a","history_flag":false,"content_hash":"sha256:..."}]}
```

`items` 必须 10 条（全失败则不写）；`status` 为 `proposed|confirmed|retracted|expired`。总分固定 `round(0.3*hot_normalized + 0.4*relevance + 0.3*value,2)`；`relevance`、`value` 是写作评估器返回的 0–100 分，缺失即候选失败，不以 0 偷过。`hot_normalized` 复用上游三源算法：每 source 按原始 `hot` 降序，n 条第 r 名为 `round(100*(n-r)/n,1)`，再全局降序取 limit；不得另写归一化。

### 2.2 热榜快照 `hotspots/YYYY-MM-DD.json`

```json
{"version":1,"date":"2026-10-26","captured_at_utc":"2026-10-26T01:00:00Z","captured_at_local":"2026-10-26T09:00:00+08:00","sources":["weibo","toutiao"],"sources_failed":["baidu"],"last_success_at":"2026-10-26T01:00:00Z","items":[{"title":"...","source":"weibo","hot":123,"hot_normalized":100.0,"url":"https://...","description":"..."}],"stale_sources":[]}
```

调用 `python -m wewrite.commands.fetch_hotspots --limit 20`（无 shell，硬超时），解析 stdout JSON；全源失败不覆盖上一份成功快照。部分失败合并成功源并标 `stale_sources`，候选保存 `snapshot_sha256`，所以同日刷新不会悄悄改变已生成候选的来源。只保留最近 30 天；MVP 手动刷新/补抓，定时器二期。

### 2.3 Job `jobs/<job_id>.json`

```json
{"version":1,"job_id":"j-01J...","run_id":"20261026-010203-a1b2c3","status":"queued","attempt":1,"cancel_requested":false,"step":"topic","progress":{"kind":"step","completed":0,"total":4},"images":[{"index":0,"status":"pending","output":"runs/.../images/0.png","error":null}],"created_at":"...Z","updated_at":"...Z","heartbeat_at":null,"last_error":null}
```

`status`=`queued|running|succeeded|failed|cancelled|unknown`；step=`topic|write|review|visual|done`。P6 唯一结论是采用“批处理中”：`generate_batch` 调用前一次写 `visual/running`，整批返回后按对齐结果一次性写每张 `succeeded|failed`，不伪造逐图百分比；页面只能显示 `completed/total` 为 0/批次总数，失败图可人工重试一次。取消只写 `cancel_requested`，worker 在步骤边界检查；在途请求不承诺可中断。

### 2.4 发布 token `publish/<token_id>.json`

```json
{"version":1,"token_id":"pt-...","token_hash":"sha256:...","run_id":"...","title_hash":"sha256:...","body_hash":"sha256:...","preflight_revision":"sha256:...","expires_at":"...Z","status":"issued","attempt_fence":1,"created_at":"...Z","consumed_at":null}
```

明文 token 仅在签发响应出现一次，磁盘只存 hash；TTL 10 分钟，绑定 run、正文/标题/图片 hash、预检 revision。消费在锁内 compare-and-swap 为 `in_flight`，调用微信成功写 `succeeded/media_id`；异常写 `unknown`，不可创建新 token 重复写。

### 2.5 配置投影

```json
{"version":1,"theme":"professional-clean","image":{"provider":"fake","model":"","max_cost":0},"wechat":{"appid_configured":true,"secret_configured":true,"author":"编辑"},"writer":{"provider":"deepseek","model":"deepseek-chat","api_key_configured":false},"permissions":{"publish":false},"env_managed":["wechat.secret"]}
```

响应永远只给 `*_configured`，不回显明文或末四位。写入 `config.yaml` 0600；secret/api_key 空值表示保持原值，显式 clear 需二次确认。真实环境变量名沿用上游：`WECHAT_APPID`、`WECHAT_SECRET`、`WECHAT_AUTHOR`、`WEWRITE_IMAGE_PROVIDER`、`WEWRITE_IMAGE_API_KEY`、`WEWRITE_IMAGE_MODEL`、`WEWRITE_THEME`；被环境接管字段写入返回 `409 ENV_MANAGED`。

## 3. HTTP 契约 `/api/v1`

所有 JSON 请求要求 `Content-Type: application/json`、同源 `Origin`、`Sec-Fetch-Site: same-origin` 和启动时内存中的 `X-CSRF-Token`；无 CORS。Host 只允许 `127.0.0.1:<port>`、`[::1]:<port>`、`localhost:<port>`，拒绝其它 Host 防 DNS rebinding。`GET` 只读，刷新热榜使用 `POST`。错误统一 `{code,message,details?}`；稳定码如下：`RUN_ID_INVALID`/400、`PATH_FORBIDDEN`/403、`NOT_FOUND`/404、`VALIDATION_ERROR`/422、`CONFLICT`/409、`ENV_MANAGED`/409、`CANDIDATE_EXPIRED`/409、`JOB_NOT_RETRACTABLE`/409、`PUBLISH_DISABLED`/403、`PREFLIGHT_FAILED`/422、`TOKEN_INVALID`/409、`PUBLISH_UNKNOWN`/409、`UPSTREAM_ERROR`/502、`BUSY`/503。

| 方法与路径 | 请求 | 成功响应与幂等语义 |
|---|---|---|
| `GET /runs?query=` | query 可选 | `{runs:[{run_id,title,status,mode,artifacts:{name:boolean},publish}]}`；文件存在性实时判定。 |
| `GET /runs/{run_id}` | 仅合法 run_id | 只读安全投影，附 topic、steps、editorial、visual、permissions；不调用上游 `load_run`。 |
| `GET /runs/{run_id}/artifact/{name}` | name 白名单 | `{name,sha256,content}` 或 `404`；禁止任意下载。 |
| `POST /preview` | `{run_id,source:"draft|article|illustrated_article",theme}` | `{html,paste_safe_html,title,digest,source_sha256}`；同输入 hash 稳定，可缓存；使用 `WeChatConverter`。 |
| `POST /hotspots/refresh` | `{date?:"YYYY-MM-DD",limit?:20}` | `{snapshot,items,sources_failed,last_success_at}`；同日成功写替换，失败不覆盖。 |
| `GET /hotspots/{date}` | 日期 | 快照 JSON；超 30 天或无文件 `NOT_FOUND`。 |
| `POST /candidates` | `{date,style_revision}` | `{candidate_set_id,expires_at,items}`；相同 snapshot hash + style revision 返回既有集。 |
| `POST /candidates/{id}/confirm` | `{candidate_id,max_images:0..10,max_image_cost>=0,mode,visual_mode,allow_publish?:false}` | 锁内校验 `proposed`、未过期、hash；调用上游 `create_run` 写 run，再写 `queued` job，返回 `{run_id,job_id}`。这是启动动作，不另设 start。 |
| `POST /candidates/{id}/retract` | 无 | 仅 `queued` 且 worker 尚未 CAS 到 `running` 时将候选置 `retracted`；否则 `JOB_NOT_RETRACTABLE`。 |
| `GET /jobs/{job_id}` | 无 | job 全量投影，含 step/image 状态；前端按需轮询，不推送伪进度。 |
| `POST /jobs/{job_id}/cancel` | 无 | 幂等设置 `cancel_requested=true`；已终态返回当前状态。 |
| `POST /jobs/{job_id}/resume` | 无 | 仅失败/取消 job；复用 `wewrite.resume_run(run_id)`，保留已完成产物，生成新 attempt。 |
| `POST /jobs/{job_id}/images/retry` | `{indices:[0..9]}` | 每个失败索引最多一次人工重试；不重试成功图，超限 `CONFLICT`。 |
| `GET /runs/{run_id}/publish/preflight` | 无 | `{ok,revision,checks:[{name,ok,code,message}]}`；标题非空、digest UTF-8 字节数 <=120、正文 200–20000 字符、图片 <=10 且可读、表格 <=4 列、cover 必需、权限/审稿通过。 |
| `POST /runs/{run_id}/publish/token` | `{checked:true,title}` | 预检通过、标题完全相同才签发一次性 token；返回 `{token,expires_at,revision}`，不落明文。 |
| `POST /runs/{run_id}/publish` | `{token,title,checked:true}` | 锁内消费 token；上传正文内本地图片 (`upload_image`)，封面 (`upload_thumb`)，最后 `create_draft`。成功 `{status:"succeeded",media_id}`；超时 `{status:"unknown"}`，之后禁止重复提交。 |
| `POST /runs/{run_id}/publish/reconcile` | `{media_id}` | 仅用户手动点击，调用只读 `get_draft` 比对 HTML hash；无 media_id 只返回人工核对指引，不自动写。 |
| `GET /config` | 无 | 配置投影与 `env_managed`，无 secret。 |
| `PUT /config` | `{theme?,image?,wechat:{appid?,secret?,author?},writer? ,clear:[...]}` | 原子写；secret/API key 只写不读，env 接管 409。 |

发布的锁顺序固定为 `platform.lock` → token 文件 → run 文件。`confirm` 顺序为候选状态 CAS、创建 run、创建 queued job 后释放；worker 取得同一锁把 job `queued→running` 作为不可撤回原子边界。任一崩溃由恢复扫描修复；若 run 已建而 job 未建，按候选 hash journal 补建同一 job，不重复 `create_run`。

摘要判定为 `len(digest.encode('utf-8')) <= 120`：ASCII 120 通过、121 拒绝；汉字 40（120 字节）通过、41 拒绝；UTF-8 emoji 30（120 字节）通过、31 拒绝。

## 4. 目录骨架与接口

```
wewrite-platform/
  pyproject.toml
  app/main.py                 # FastAPI、Host/CSRF、中间件、静态挂载
  app/api/{runs,hotspots,candidates,jobs,publish,config,preview}.py
  app/domain/{models,errors,preflight,scoring,state_machine}.py
  app/storage/{atomic,locks,paths,projections}.py
  app/adapters/{wewrite,hotspots,writer,wechat,image}.py
  app/worker/{queue,recovery,executor}.py
  web/{index.html,app.js,styles.css}
  tests/{unit,integration,browser,fixtures/WEWRITE_HOME}/
  Dockerfile docker-compose.yml
```

深模块接口集中复杂性：`RunProjection.list/get_artifact`（固定白名单与路径安全）、`JobStore.transition`（合法状态转移与原子写）、`WeWriteAdapter`（版本锁定及上游异常归一化）、`Publisher.commit_once`（token fence、上传顺序、unknown 规则）、`Preflight.check`（唯一发布门）。路由只做 HTTP 映射，不直接读文件或调用上游。

## 5. M1–M4 顺序与 DoD

1. `feat/m1-skeleton`：空 `WEWRITE_HOME` 可启动首页、健康检查、锁/原子写、run 安全投影、配置投影、所有路由返回稳定错误；fake adapter 下 `pytest` 一键通过，Docker healthcheck 为 200。
2. `feat/m2-preview-hotspots`：热榜 CLI 适配、30 天快照/部分失败、候选评分与过期门、P3 预览 sandbox；fixture 能完成“刷新→10 候选→预览”。
3. `feat/m3-topic-gate`：confirm 锁内创建 run+queued job、撤回 CAS、job 恢复/取消/批处理图片状态、P7 全量预检与 token（fake publish）；覆盖崩溃和重复请求。
4. `feat/m4-generate-publish`：真实/桩 writer、`generate_batch`、图片上传替换、微信 draft/get 对账、P8 配置和 Docker；无凭据走 dry-run，凭据存在才允许真实写。

每个分支先合入前一分支的测试；DoD 必须包含接口契约测试、fixture 流程和 `python -m pytest -q`。M1 不要求真实网络或真实微信。

## 6. 测试与本地一键运行

- 单元：评分公式、UTF-8 digest、状态机、路径/symlink、原子写、token fence、预检每个失败项。
- 集成：`tmp_path/WEWRITE_HOME` + fake `WeWriteAdapter`，覆盖同日全失败不覆盖、confirm 幂等、queued 撤回、running 拒绝、批处理部分失败、unknown 禁止重复写、env 409。
- 浏览器：Playwright 验证 P1–P8 关键点击、iframe `sandbox` 无 `allow-scripts`、失败图和发布按钮状态；不依赖公网。
- `make test`（或 `python -m pytest -q`）先创建临时 HOME、启动 `uvicorn`、运行接口/Playwright、清理临时目录。fixture 至少包含空 HOME、三状态 run、三源部分失败快照、成功/失败图片 manifest、publishable 和 blocked 两篇文章。

## 7. Docker 与运维

`python:3.11-slim` 安装锁定依赖，非 root 用户 `wewrite`；`WEWRITE_HOME=/data/wewrite` 挂载命名卷，entrypoint 检查目录属主/权限后启动 `uvicorn app.main:app --host 0.0.0.0 --port 8765 --workers 1`。compose：

```yaml
services:
  workbench:
    build: .
    ports: ["127.0.0.1:8765:8765"]
    volumes: ["wewrite_home:/data/wewrite"]
    environment: ["WEWRITE_HOME=/data/wewrite"]
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz')"], interval: 30s, timeout: 3s, retries: 3}
volumes: {wewrite_home: {}}
```

密钥只通过 Docker secrets 或环境变量注入，不写镜像层、不打印日志；应用启动时只计算 configured 布尔。宿主端口始终 loopback；若用户主动反代到公网，产品不提供账号体系，视为不支持部署形态。

## 8. 风险与降级（最多五条）

1. 上游 `generate_batch` 无回调且内置重试：MVP 显示批处理中、整批回填；严格费用/零重试场景 fail closed，等待上游可配置重试 PR。
2. 外部 CLI 与平台并发写状态：运行前提声明唯一 writer；检测到锁/版本冲突返回 `BUSY`，不覆盖文件。
3. 微信请求超时无法判断是否写入：token 进入 `unknown` 永久封存，提供手动 `get_draft`/后台核对，不自动重试。
4. 真实 provider 或凭据缺失：fake/dry-run 完成全链路，真实写入口保持禁用；不伪造 publishable。
5. 同日热榜部分源失败：保留上一成功源并标 stale，候选绑定快照 hash；全失败不生成候选。

## 9. S1 冲突处理记录

以功能点为准：`get_draft` 是手动只读核对；密钥只返回 configured 且无末四位；生图只有人工一次重试；热榜 scheduler 延后二期。P8 的 appid/author 可编辑但 secret/API key 只写不读。P6 的批处理中与逐图展示通过批次边界解决；P5 的 confirm 同步创建 queued job，`queued→running` 才是不可撤回点，既保留撤回窗口又不增加用户 start 按钮。
