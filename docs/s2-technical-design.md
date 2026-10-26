# WeWrite 工作台 S2 技术详细设计

本文是 JEE-1908 的编码基线。目标是单机、单用户、loopback 上把热榜、选题、写作、预览、配图和人工确认写入微信草稿箱串成一条可恢复流水线。数据库、账号体系、自动发布和复制 `wewrite` 实现均不在范围内。

## 1. 选型与约束

- 后端固定 `FastAPI` + `uvicorn`，单进程、`workers=1`；长任务由进程内 asyncio 调度器串行消费一个 durable job 队列，外部慢调用使用受控线程/子进程，重启后由 job 文件恢复。
- 前端固定原生 HTML/CSS/ES modules，无 Node/Vite 构建；FastAPI 同进程挂载 `web/` 静态文件。这样 M1 在只有 Python 的空仓即可运行，减少前端依赖和跨源问题。
- Python 依赖 `wewrite==4.2.1`（Python >=3.11）。平台只调用其公开函数/CLI：`runs`、`WeChatConverter`/`make_paste_safe`、`load_theme`、`generate_batch`、`wewrite.toolkit.wechat_api.upload_image`、`wewrite.toolkit.wechat_api.upload_thumb`、`create_draft`/`get_draft`、`llm-write`。不得复制 converter/theme/publisher/image_gen/runs；需要能力时提上游 PR。
- 状态源为 `$WEWRITE_HOME` 下 YAML/JSON/Markdown/图片文件；不引入 SQLite、Redis、Celery。所有写入由 `platform.lock`（`flock`）保护，临时文件同目录写入、`fsync` 后 `os.replace`，目录也 `fsync`。
- 单机前提是平台是该 `WEWRITE_HOME` 的唯一写者；外部 `wewrite` CLI 只读或先停止平台。上游 `_atomic_write_text` 自身没有锁，平台不能宣称能约束外部进程。
- 监听默认 `127.0.0.1:8765`。Docker 进程监听容器 `0.0.0.0`，compose 只映射宿主 `127.0.0.1:8765`。

### 上游真实接口差异

`validate_run_id` 是 `^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$`；`load_run` 会升级并回写旧状态，HTTP GET 只能用平台自己的 `safe_load_run`。平台只接受 `mode=draft|complete`，`mode=publish` 直接 `403 PUBLISH_DISABLED`；调用上游 `create_run` 时固定 `allow_publish=false`，`run_id` 由上游生成后回填，不能伪造或预传。confirm 先写 intent journal（候选 `content_hash`、snapshot hash、style revision、客户端 correlator），再调用 `create_run`；恢复时只允许把唯一匹配的已创建 run 回填，无法唯一匹配的移入 `orphans/` 待人工核对，禁止二次调用。`generate_batch` 只返回整批结果且无回调；单图函数内部每 provider 最多重试两次并 fallback，平台不再叠加重试，若严格零重试的真实 provider 未得到上游配置能力则 fail closed（`IMAGE_RETRY_POLICY_UNSUPPORTED`）。`llm-write` 退出码 3 是 `WRITER_NOT_CONFIGURED`，4 是 `WRITER_FAILED`；分别写入 `last_error.code`，4 不覆盖已有正文。`llm-write` 是可选文件到文件适配器，未配置时不伪造正文。

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
  intents/<intent_id>.json
  orphans/<intent_id>/
  publish/<token_id>.json
  audit/YYYY-MM-DD.jsonl
```

读路径必须在打开时校验，artifact 与 preview 共用同一个 `safe_open_run_file` 函数。进程启动时记录 `$WEWRITE_HOME` 的 `st_dev`/`st_ino`；每次读取以 `os.open(WEWRITE_HOME, O_DIRECTORY|O_NOFOLLOW)` 重新取得状态根 fd，并与启动值比对，不一致即 `403 PATH_FORBIDDEN`。从该 fd 起逐级 `os.open(..., dir_fd=parent_fd, flags=O_DIRECTORY|O_NOFOLLOW)` 打开 `runs`、合法 `run_id` 目录；任一级 `ELOOP`、`ENOTDIR` 或非目录均统一为 `403 PATH_FORBIDDEN`，不得回退为按路径打开。最后仅允许固定产物名白名单 `brief,article,draft,illustrated_article,review_report,images_manifest,sources,preview`，用 `os.open(name, O_NOFOLLOW|O_RDONLY, dir_fd=run_fd)` 打开，`fstat` 确认 `S_ISREG` 且 `st_nlink >= 1`，全程从该 fd（如 `os.fdopen`）读取，读完不得再按路径打开。禁止绝对路径、`..`、符号链接、`pathlib` 拼路径和先 `lstat`/`realpath` 后按路径打开的竞态写法。

平台自身每次 JSON/YAML 写入执行：取得全局锁 → 读取并校验版本 → 同目录临时文件 `0600` → UTF-8 写入、`fsync` → `os.replace` → 父目录 `fsync` → 释放锁。单文件锁不能替代全局锁。平台调用上游写接口时同样持有全局锁，返回后对受影响文件及目录补 fsync；不得把上游无锁实现说成天然具备这些保证。崩溃恢复扫描 `jobs/*.json`：`running` 无心跳超过 120 秒转 `unknown`（外部调用可能已发生），`queued` 可撤回；绝不根据未知状态重放微信写入。

### 2.1 候选集 `candidates/<date>-<hash>.json`

```json
{"version":1,"candidate_set_id":"20261026-a1b2c3","set_status":"open","created_at":"2026-10-26T08:00:00Z","expires_at":"2026-10-27T08:00:00Z","timezone":"Asia/Shanghai","hotspot_snapshot":"hotspots/2026-10-26.json","snapshot_sha256":"...","style_revision":"sha256:...","items":[{"id":"c01","status":"proposed","title":"标题（20-28字）","angle":"切入角度","scores":{"hot":92.1,"hot_normalized":100.0,"relevance":82.0,"value":76.0,"total":84.63},"framework":"问题-证据-行动","source":"微博","source_id":"weibo","source_url":"https://example.invalid/a","history_flag":false,"content_hash":"sha256:..."}]}
```

`items` 必须 10 条（全失败则不写）；item `status` 为 `proposed|confirmed|retracted|expired`；集合级 `set_status` 为 `open|confirmed|superseded|deleted|expired`（默认 open），两种状态不可混用。上例仅展示一个 item，实际成功响应必须包含 10 条。总分固定 `round(0.3*hot_normalized + 0.4*relevance + 0.3*value,2)`；`relevance`、`value` 是写作评估器返回的 0–100 分，缺失即候选失败，不以 0 偷过。`hot_normalized` 复用上游算法：每 source 按原始 `hot` 降序，0-based rank 为 r 时 `round(100*(n-r)/n,1)`，各源归一化完成后才全局降序并按 `--limit` 截断；不得另写归一化。

集合确认后关闭，撤回不重新开放；force 只取代 open 集，保留旧候选及 run。snapshot hash 与 style revision 对应的数据不可就地修改，快照覆盖前先为仍被引用的内容留存 hash 副本。

### 2.2 热榜快照 `hotspots/YYYY-MM-DD.json`

```json
{"version":1,"date":"2026-10-26","captured_at_utc":"2026-10-26T01:00:00Z","captured_at_local":"2026-10-26T09:00:00+08:00","sources":["weibo","toutiao"],"sources_failed":["baidu"],"last_success_at":"2026-10-26T01:00:00Z","count":1,"error":null,"items":[{"title":"...","source":"微博","source_id":"weibo","hot":123,"hot_normalized":100.0,"url":"https://...","description":"..."}],"stale_sources":[]}
```

调用 `python -m wewrite.commands.fetch_hotspots --limit 20`（无 shell，硬超时），解析 stdout JSON；`--limit` 只在各源归一化、全局排序之后截断。`date` 仅用于命名快照，抓取始终是当下数据，不宣称历史回抓。条目的 `source` 是中文展示名 `微博|今日头条|百度`，`sources`/`sources_failed` 是英文源 ID。全源失败即使进程退出码为 0，也必须检查 `error` 或 `count=0`，不覆盖上一份成功快照。部分失败合并成功源并标 `stale_sources`，候选保存 `snapshot_sha256`；只保留最近 30 天，MVP 手动刷新/补抓。

### 2.3 Job `jobs/<job_id>.json`

```json
{"version":1,"job_id":"j-01J...","run_id":"20261026-010203-a1b2c3","status":"queued","attempt":1,"cancel_requested":false,"step":"topic","progress":{"kind":"step","completed":0,"total":4},"images":[{"index":0,"status":"pending","output":"runs/.../images/0.png","error":null}],"created_at":"...Z","updated_at":"...Z","heartbeat_at":null,"last_error":null}
```

`status`=`queued|running|succeeded|failed|cancelled|unknown`；step=`topic|write|review|visual|done`。P6 唯一结论是采用“批处理中”：`generate_batch` 调用前一次写 `visual/running`，整批返回后按对齐结果一次性写每张 `succeeded|failed`，不伪造逐图百分比；页面只能显示 `completed/total` 为 0/批次总数，失败图可人工重试一次。取消只写 `cancel_requested`，worker 在步骤边界检查；在途请求不承诺可中断。

### 2.4 发布 token `publish/<token_id>.json`

```json
{"version":1,"token_id":"pt-...","token_hash":"sha256:...","run_id":"...","title_hash":"sha256:...","body_hash":"sha256:...","preflight_revision":"sha256:...","expires_at":"...Z","status":"issued","attempt_fence":1,"created_at":"...Z","consumed_at":null}
```

明文 token 仅在签发响应出现一次，磁盘只存 hash；TTL 10 分钟，绑定 run、正文/标题/图片 hash、预检 revision。消费在锁内 compare-and-swap 为 `in_flight`，调用微信成功写 `succeeded/media_id`；异常写 `unknown`，不可创建新 token 重复写。另存 `publish/attempts/<run_id>.json` 的 durable fence（attempt、token_id、revision、正文/标题/图片/封面 hash、状态），与 token 用 journal 恢复一致性；未完成 journal 一律 unknown。签发前检查 fence，in_flight/unknown/succeeded 均禁止换 token 重发；撤销 issued token 后只有重新授权和重新预检才能签新 token。真正调用 create_draft 前重取当前权限、revision 和绑定内容，任何变化均拒绝，已在途的远端请求不能承诺撤回。

### 2.5 配置投影

```json
{"version":1,"theme":"professional-clean","image":{"provider":"fake","model":"","base_url":"","estimated_cost_per_image":0,"max_cost":0,"api_key_configured":false},"wechat":{"appid":"wx...","appid_configured":true,"secret_configured":true,"author":"编辑"},"writer":{"provider":"deepseek","model":"deepseek-chat","api_key_configured":false},"permissions":{"publish":false},"env_managed":[{"field":"wechat.secret","env":"WECHAT_SECRET"}]}
```

secret/API key 永远只给 `*_configured`，不回显明文或末四位；appid、author、provider、model 是可读普通字段。`permissions.publish=false` 仅只读默认值，不允许通过全局配置开启任何 run 的发布权限。writer 为环境只读配置，使用 `WEWRITE_WRITER_API_KEY/PROVIDER/BASE_URL/MODEL/TEMPERATURE/MAX_TOKENS`，不接受 PUT。写入 `config.yaml` 0600；secret/api_key 空值表示保持原值，显式 clear 需二次确认。真实环境变量名沿用上游：`WECHAT_APPID`、`WECHAT_SECRET`、`WECHAT_AUTHOR`、`WEWRITE_IMAGE_PROVIDER`、`WEWRITE_IMAGE_API_KEY`、`WEWRITE_IMAGE_MODEL`、`WEWRITE_THEME`；被环境接管字段写入返回 `409 ENV_MANAGED`。接管以变量存在为准（含空串），`env_managed` 同时返回字段与变量名映射。`clear` 仅允许 `wechat.secret|image.api_key`，必须同时 `clear_confirmed=true`，null 不表示清除；字段缺失不改，密钥空串保持，未知字段拒绝。image.providers 链及 cover_template 只读，image.base_url、estimated_cost_per_image 可写。

## 3. HTTP 契约 `/api/v1`

表中路径统一加 `/api/v1` 前缀，唯一例外是根路径 `/healthz`。未带业务参数的变更请求仍发送 JSON `{}`；请求表中 `?` 表示可选字段，其余必填。集合/任务 ID 只允许服务端生成的格式，不接受路径。

`GET /api/v1/bootstrap` 返回 `{csrf_token,version,server_started_at}`；token 只在进程内存保存，重启失效，前端遇 `403 CSRF_INVALID` 自动重新取得一次。只有 POST/PUT/DELETE 强制 `Content-Type: application/json`、同源 `Origin`、`Sec-Fetch-Site: same-origin` 和 `X-CSRF-Token`；普通 GET 与 `/healthz` 只做 Host 校验。唯一例外是会触发外部抓取的 `GET /api/v1/hotspots?refresh=1`：还必须校验 `Origin` 为 `http://127.0.0.1:<port>`、`http://localhost:<port>` 或 `http://[::1]:<port>` 之一且 `Sec-Fetch-Site: same-origin`；缺失或为 `cross-site`/`same-site` 等其它值时，在调用 fetcher 前返回 `403 CSRF_INVALID` 且 `details.reason=refresh_requires_same_origin`，不得发起出网请求。浏览器同源 `fetch` 会自动携带这两个请求头，不需要 CSRF token。无 CORS。Host 只允许 `127.0.0.1:<port>`、`[::1]:<port>`、`localhost:<port>`，拒绝其它 Host 防 DNS rebinding。错误统一 `{code,message,details?}`；稳定码如下：`RUN_ID_INVALID`/400、`PATH_FORBIDDEN`/403、`NOT_FOUND`/404、`VALIDATION_ERROR`/422、`CONFLICT`/409、`ENV_MANAGED`/409、`CANDIDATE_NOT_ACTIONABLE`/409、`JOB_NOT_RETRACTABLE`/409、`PUBLISH_DISABLED`/403、`PREFLIGHT_FAILED`/422、`TOKEN_INVALID`/409、`PUBLISH_UNKNOWN`/409、`UPSTREAM_ERROR`/502、`BUSY`/503、`CSRF_INVALID`/403。候选不可操作统一 `CANDIDATE_NOT_ACTIONABLE`，`details.reason` 为 `not_proposed|expired|hash_changed|set_closed`。

| 方法与路径 | 请求 | 成功响应与幂等语义 |
|---|---|---|
| `GET /api/v1/bootstrap` | 无 | `{csrf_token,version,server_started_at}`，服务重启失效。 |
| `GET /runs?query=` | query 可选 | `{runs:[{run_id,title,status,mode,artifacts:{name:boolean},publish}]}`；文件存在性实时判定。 |
| `GET /runs/{run_id}` | 仅合法 run_id | 只读安全投影，附 topic、steps、editorial、visual、permissions、`publish:{status,media_id,unknown_at}`；不调用上游 `load_run`。 |
| `GET /runs/{run_id}/artifact/{name}` | name 白名单 | `{name,sha256,content}` 或 `404`；禁止任意下载。 |
| `POST /preview` | `{run_id,source:"draft|article|illustrated_article",theme}` | `{html,paste_safe_html,title,digest,source_sha256,filtered_count}`；同输入 hash 稳定，可缓存；使用 `WeChatConverter` 后过滤 `<script>`、事件属性和 `javascript:` URL，iframe 固定 `sandbox` 且不含 `allow-scripts`。 |
| `GET /healthz` | 无 | `{status:"ok",version}`；只读、只 Host 校验。 |
| `GET /themes` | 无 | `{themes:[{name,description}]}`，名称来自 `list_themes`，不接受路径。 |
| `GET /hotspots?limit=20&refresh=1` | `limit` 1–100；普通请求只 Host 校验，`refresh=1` 另需同源 `Origin` + `Sec-Fetch-Site: same-origin` | `refresh=1` 先完成同源校验再抓取并返回当下数据，不写快照或 `last_success_at`；缺失/错误请求头返回 `403 CSRF_INVALID`、`details.reason=refresh_requires_same_origin` 且不调用 fetcher。浏览器同源 `fetch` 自动携带请求头，不需 CSRF token。需要持久化时调用 `POST /hotspots/refresh`。否则返回当前快照 `{date,items,sources,sources_failed,last_success_at,error,count}`。 |
| `GET /hotspot-snapshots` | 无 | `{dates:["YYYY-MM-DD"]}`，仅最近 30 天；`GET /hotspots/{date}` 返回对应快照。 |
| `GET /hotspots/{date}` | `YYYY-MM-DD` | 返回已保存命名快照，缺失或超过保留期 `404 NOT_FOUND`。 |
| `POST /hotspots/refresh` | `{date?:"YYYY-MM-DD",limit?:20}` | 同一刷新服务；`date` 只命名文件，抓取当下数据。全失败不覆盖。 |
| `GET /candidates?date=&status=` | 过滤可选 | 返回候选集摘要及其 `items`；页面刷新可回读。 |
| `GET /candidates/{id}` | 候选集 ID | 返回完整集、过期/关闭状态和 snapshot hash。 |
| `POST /candidates` | `{date,style_revision,force?:false}` | 相同 snapshot hash + style revision 幂等返回既有 open 集；`force=true` 写审计并把旧集置 `superseded`，不删除候选或 run。 |
| `DELETE /candidates/{id}` | 无 | 仅从未确认、无 intent/job/run 关联的 open 集可置 deleted 墓碑；已确认后撤回也拒绝删除；否则 `409 CANDIDATE_NOT_ACTIONABLE` reason=`set_closed`，不删除任何 run。 |
| `POST /candidates/{id}/confirm` | `{candidate_id,content_hash,max_images:0..10,max_image_cost>=0,mode:"draft|complete",visual_mode}` | 请求必带 `Idempotency-Key`；锁内校验 proposed/未过期/hash 后写 intent；调用上游 `create_run(...,allow_publish=false)`，回填其 run_id，再写 queued job，返回 `{run_id,job_id}`。`mode=publish` 为 `403 PUBLISH_DISABLED`。 |
| `POST /candidates/{id}/retract` | `{candidate_id}` | 仅 `queued` 且 worker 尚未 CAS 到 `running` 时置 `retracted`；否则 `JOB_NOT_RETRACTABLE`。 |
| `GET /jobs/{job_id}` | 无 | job 全量投影，含 step/image 状态；前端按需轮询，不推送伪进度。 |
| `POST /jobs/{job_id}/cancel` | 无 | 幂等设置 `cancel_requested=true`；已终态返回当前状态。 |
| `POST /jobs/{job_id}/resume` | 无 | 仅失败/取消 job；复用 `wewrite.resume_run(run_id)`，保留已完成产物，生成新 attempt。 |
| `POST /jobs/{job_id}/accept-existing` | `{confirm:true}` | 仅记录用户接受已有结果的审计事件并进入详情/预览，不改变完成状态、不伪造缺失产物；随后仍受预检约束。 |
| `POST /jobs/{job_id}/images/retry` | `{indices:[0..9]}` | 每个失败索引最多一次人工重试；不重试成功图，超限 `CONFLICT`。 |
| `GET /runs/{run_id}/publish/preflight` | 无 | 只读幂等 `{ok,revision,checks:[{name,ok,code,message}]}`；标题非空、digest UTF-8 字节数 <=120、正文 200–20000 字符、图片 <=10 且可读、表格 <=4 列、cover 必需、权限/审稿通过。revision 是被检查内容投影 hash。 |
| `PUT /runs/{run_id}/publish/permission` | `{allow:true|false,reason?}` | 仅用户主动操作；调用上游 `set_publish_permission`，写 `audit/*.jsonl`（actor=`local-user`、UTC 时间、run_id、前后值、reason）；返回 `{permissions:{publish},revision}`。撤销立即作废该 run 未消费 token。 |
| `POST /runs/{run_id}/publish/token` | `{revision,checked:true,title}` | revision 必须匹配 GET 预检，权限当前为 true 且标题完全相同才签发 token；不匹配 `409 CONFLICT`；其它预检项失败返回 `422 PREFLIGHT_FAILED`。 |
| `POST /runs/{run_id}/publish` | `{token,title,checked:true}` | 锁内消费 token，并再次核对权限、revision、内容绑定；按固定顺序调用 `wewrite.toolkit.wechat_api.upload_image` 上传正文图片，再调用 `wewrite.toolkit.wechat_api.upload_thumb` 上传封面，最后调用 `create_draft`（封面缺失时上游会 `ValueError`，不得先创建草稿）。成功 `{status:"succeeded",media_id}`；超时 `{status:"unknown"}`，之后禁止重复提交。 |
| `POST /runs/{run_id}/publish/reconcile` | `{media_id}` | 有 media_id 才调用 `get_draft`；双方用 `html_to_plaintext` 归一化后比较文本 hash，只有文本不同才判不一致。仅文本核对，不代表图片与排版一致；无 media_id 只返回人工核对指引。保存提交 HTML、双方归一化文本 hash 与核对时间；对账不能解除 unknown 的重发禁令。 |
| `GET /config` | 无 | 配置投影与 `env_managed`，无 secret。 |
| `PUT /config` | `{theme?,image:{provider,model,api_key,base_url,max_cost,estimated_cost_per_image}?,wechat:{appid,secret,author}?,clear:[...]}` | 白名单字段原子写；`appid`/`author` 可编辑，`secret`、`api_key` 允许只写不读；writer 环境配置整体只读并拒绝写入；空值保持原值，显式 clear 二次确认；环境变量接管字段返回 `409 ENV_MANAGED`，整包失败不局部提交。 |
| `GET /style` | 无 | 返回非密钥 style 投影、revision、四步 onboarding 状态。 |
| `PUT /style` | `{name,industry,target_audience,topics,tone,voice,word_count,content_style,writing_persona,theme,blacklist,cover_style,author}` | 白名单字段原子写 `style.yaml` 0600；缺失时显式保存默认值，不静默写入。 |

发布的锁顺序固定为 `platform.lock` → token 文件 → run 文件。confirm 由单写者进程持有 owner 锁；`intents/<id>.json` 在调用前持久化 `{idempotency_key,correlator,candidate_set_id,candidate_id,content_hash,snapshot_sha256,style_revision,creation_settings,run_ids_before,job_id,phase,run_id}`，`phase` 依次为 `prepared|call_started|bound|committed|orphaned`。候选状态 CAS 后写 `call_started`，只调用一次上游 `create_run`；返回即写 `bound` 和 run_id，再创建 queued job 并写 `committed`。同一 idempotency key 重放返回原 job，不同 key 对已确认候选返回 `409 CONFLICT`。恢复扫描计算 `runs_now - run_ids_before`，仅当差集恰有一个且其 topic/mode/visual/permissions 与 intent 严格匹配才回填；0 个、多于一个、校验失败或损坏均把 intent 与疑似新 run 移入 `orphans/<intent-id>/`，关闭候选集并等待人工核对，绝不二次调用 `create_run`。授权撤销时令未消费 token `revoked`。

摘要预检取值固定为 `state.seo.digest`（editorial 手填字段存在时，即使为空串也优先）→否则 `converter.digest`；title 同理用 `state.seo.title` → `converter.title`，提交和预检必须使用同一投影，revision 包含最终取值。手填值不因 converter 已截断而自动通过。判定为 `len(digest.encode('utf-8')) <= 120`：ASCII 120 通过、121 拒绝；汉字 40（120 字节）通过、41 拒绝；UTF-8 emoji 30（120 字节）通过、31 拒绝。`llm-write` 退出码 3/4 分别写 `last_error.code=WRITER_NOT_CONFIGURED|WRITER_FAILED`；调用始终把输出指向 staging 临时文件，只有退出 0 且正文校验通过才原子推广，退出 4 不得覆盖既有正文。

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
  web/{index.html,app.js,styles.css} # 原生静态文件，静态根为 web/
  tests/{unit,integration,browser,fixtures/WEWRITE_HOME}/
  Dockerfile docker-compose.yml
```

依赖方向固定为 api → domain/worker → storage/adapters；adapter 不反向依赖 HTTP。核心接口为 `safe_read_artifact(run_id, name) -> bytes`、`ConfirmGate.confirm(set_id, candidate_id, content_hash, idempotency_key, settings) -> (run_id, job_id)`、`PublishGate.set_permission(run_id, allow, reason)`、`Preflight.check(run_id) -> (revision, checks)`、`Publisher.commit_once(run_id, token, title)`。

深模块接口集中复杂性：`RunProjection.list/get_artifact`（固定白名单与路径安全）、`JobStore.transition`（合法状态转移与原子写）、`WeWriteAdapter`（版本锁定及上游异常归一化）、`Publisher.commit_once`（token fence、上传顺序、unknown 规则）、`Preflight.check`（唯一发布门）。路由只做 HTTP 映射，不直接读文件或调用上游。

## 5. M1–M4 顺序与 DoD

1. `feat/m1-skeleton`（父任务 stage 3，P1/P2）：空 `WEWRITE_HOME` 可启动首页、bootstrap/healthz、锁/原子写、run 安全投影、运行详情含 publish 投影；本阶段已交付路由返回稳定错误，不要求预造后续功能路由。可独立合并，`python -m pytest -q` 全绿；不含 P8 配置或 Docker 验收。
2. `feat/m2-preview-hotspots`（stage 4，P3/P4）：themes、preview 安全过滤、热榜 CLI 适配、30 天快照/部分失败、hotspot 日期列表与刷新查询；`refresh=1` 的跨站/无头请求必须在 fake fetcher 计数为 0 时返回 `403 CSRF_INVALID`，同源请求正常；fixture 能完成“刷新→快照→预览”。可独立合并，`python -m pytest -q` 全绿。
3. `feat/m3-topic-gate`（stage 5，P5）：候选生成/回读/删除保护/过期门、评分、force 审计、confirm intent→run→queued、撤回 CAS；只实现首个 job 的持久化与 queued 撤回，不启动 worker 消费，不包含 P6/P7。可独立合并，`python -m pytest -q` 全绿。
4. `feat/m4-generate-publish`（stage 6，P6/P7/P8/Docker）：启用 worker 自动消费既有 queued job，首次生成仅来自 confirm，不提供独立 generate 端点；resume/批处理图片、写作退出码、预检 GET、发布授权、一次性 token、微信 draft/get 文本对账、config/style、容器化。无凭据走 dry-run，凭据存在才允许真实写。可独立合并，`python -m pytest -q` 全绿。

前一阶段合入 main 后，下一分支从最新 main 开始；每个 DoD 都必须包含接口契约测试、fixture 流程和 `python -m pytest -q` 全绿。验收固定使用 `127.0.0.1:8765`，不使用机器 IP；M1 不要求真实网络或真实微信。

## 6. 测试与本地一键运行

- 单元：评分公式、UTF-8 digest、状态机、路径/symlink、原子写、token fence、预检每个失败项。
- 返修复验：bootstrap 重启失效/自动重取一次、GET 与 healthz 无 CSRF、`runs/` 被替换为 symlink 和 `runs/<run_id>/` 被替换为 symlink 均返回 `403 PATH_FORBIDDEN`、并发换 symlink 时断言读取的是已打开 fd 快照、`refresh=1` 缺失或 cross-site/same-site 头时在 fake fetcher 计数为 0 的情况下返回 `403 CSRF_INVALID` 且同源请求成功、HTML 脚本过滤计数、call_started 各崩溃点不二次创建、授权撤销废 token/提交前变化拒绝、摘要手填 120/121 字节、writer 退出码 3/4 且旧正文不变、热点全失败 exit 0 与中文来源/0-based rank、对账仅文本等，均用 fixture。
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

M4 README 必须写明：健康检查及浏览器验收使用 `http://127.0.0.1:8765`；Host 为机器 IP 或非白名单域名时返回 403，容器内监听 0.0.0.0 不等于宿主对外暴露；配置 env 接管与 style.yaml 的 0600、同目录原子替换均需验收。

部署密钥可通过 Docker secrets 或环境变量注入，页面写入的密钥只保存在挂载卷 config.yaml 0600；不写镜像层、不打印日志；应用启动时只计算 configured 布尔。宿主端口始终 loopback；若用户主动反代到公网，产品不提供账号体系，视为不支持部署形态。

## 8. 风险与降级（最多五条）

1. 上游 `generate_batch` 无回调且内置重试：MVP 显示批处理中、整批回填；严格费用/零重试场景 fail closed，等待上游可配置重试 PR。
2. 外部 CLI 与平台并发写状态：运行前提声明唯一 writer；检测到锁/版本冲突返回 `BUSY`，不覆盖文件。
3. 微信请求超时无法判断是否写入：token 进入 `unknown` 永久封存，提供手动 `get_draft`/后台核对，不自动重试。
4. 真实 provider 或凭据缺失：fake/dry-run 完成全链路，真实写入口保持禁用；不伪造 publishable。
5. 同日热榜部分源失败：保留上一成功源并标 stale，候选绑定快照 hash；全失败不生成候选。

## 9. S1 冲突处理记录

以功能点为准：`get_draft` 是手动只读核对；密钥只返回 configured 且无末四位；生图只有人工一次重试；热榜 scheduler 延后二期。P8 的 appid/author 可编辑但 secret/API key 只写不读。P6 的批处理中与逐图展示通过批次边界解决；P5 的 confirm 同步创建 queued job，`queued→running` 才是不可撤回点，既保留撤回窗口又不增加用户 start 按钮。
