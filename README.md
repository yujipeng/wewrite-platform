# wewrite-platform

为 wewrite 开发的本地可视化平台（工作台）。单机、单用户、loopback 监听，把热榜、选题、写作、预览、配图和人工确认写入微信草稿箱串成一条可恢复流水线。

设计基线见仓库内 `docs/s2-technical-design.md`（技术详细设计终版，编码基线）。

## 当前里程碑（M4）

已交付 M1（骨架 P1/P2）→ M2（P3 预览 + P4 热榜）→ M3（P5 选题确认）→ M4（P6 生成 + P7 发布闸门 + P8 配置 + 容器化）。

- M1：`GET /healthz`、`GET /api/v1/bootstrap`、`GET /api/v1/runs`、`GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/artifact/{name}`。
- P3 预览：`GET /api/v1/themes`、`POST /api/v1/preview`（复用 `safe_open_run_file`，HTML 过滤脚本/事件/`javascript:`）。
- P4 热榜：`GET /api/v1/hotspots`、`POST /api/v1/hotspots/refresh`、`GET /api/v1/hotspot-snapshots`、`GET /api/v1/hotspots/{date}`。
- P5 选题确认：候选生成/回读/删除保护/确认门/撤回（见 `app/domain/confirm_gate.py`）。
- P6 生成进行中：进程内 asyncio worker 自动消费 `queued` job（写作 → 审稿 → 视觉 → 转换预览），`POST /api/v1/jobs/{job_id}/cancel|resume|accept-existing|images/retry`，`GET /api/v1/jobs/{job_id}`。
- P7 发布闸门：`GET /api/v1/runs/{run_id}/publish/preflight`、`PUT .../publish/permission`、`POST .../publish/token`、`POST .../publish`、`POST .../publish/reconcile`。
- P8 配置：`GET/PUT /api/v1/config`、`GET/PUT /api/v1/style`。

## 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
# 本机 pypi.org 不稳定时用清华/阿里镜像（不改变依赖声明）
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple/

wewrite-platform serve --port 8765
```

浏览器打开 `http://127.0.0.1:8765`（Host 必须是 `127.0.0.1:8765` / `localhost:8765` / `[::1]:8765`，机器 IP 或其它域名会被 403 拒绝）。

状态根默认 `~/.wewrite`，可用 `WEWRITE_HOME` 覆盖。热榜补抓：`wewrite-platform hotspots refresh [--limit N]`。

## 测试

测试运行口径（venv 建在 checkout 外 + 清华镜像 + `-e ".[test]"`）唯一化在 `docs/dev-test-env.md`，跑测试一律照那里装，不要用系统 `python3` 直跑。快速版：

```bash
python3 -m venv ../.venv-test
../.venv-test/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[test]"
../.venv-test/bin/python -m pytest -q
```

fixture 在 `tests/conftest.py`；M4 新增 `tests/test_publish.py`（预检/密钥/token）与 `tests/test_worker.py`（P6 状态机）。

## Docker 部署

本机 `docker compose`（v2 插件）**不可用**，只有 `docker-compose` v1.29.2；`docker-compose.yml` 已按 v1.29 兼容语法编写（`docker-compose -f docker-compose.yml config` 校验可用）。两种写法都可用：

```bash
# 方式一：v1 二进制（本机可用）
docker-compose up -d --build

# 方式二：v2 插件（若你的机器有 docker compose）
docker compose up -d --build
```

打开 `http://127.0.0.1:8765`。健康检查与浏览器验收都用回环地址：容器内监听 `0.0.0.0`，但 compose 只把宿主 `127.0.0.1:8765` 映射进来；用机器 IP 或其它域名访问会被 403 拒绝。

### 首次配置与密钥注入（env 优先）

密钥**只写不读**：页面写入的 `wechat.secret`、`image.api_key` 只保存在挂载卷 `config.yaml`（0600），不回显明文、不打印日志、不写镜像层。环境变量优先于页面配置，且被环境变量接管的字段写入会返回 `409 ENV_MANAGED`（整包失败不局部提交）。

```bash
docker-compose run --rm -e WECHAT_APPID=wx... -e WECHAT_SECRET=... workbench wewrite-platform serve
# 或修改 compose 的 environment 段注入（推荐 Docker secrets 或 env_file）
```

支持的环境变量（与上游 wewrite 一致）：

- 公众号：`WECHAT_APPID`、`WECHAT_SECRET`、`WECHAT_AUTHOR`
- 图片：`WEWRITE_IMAGE_PROVIDER`、`WEWRITE_IMAGE_API_KEY`、`WEWRITE_IMAGE_MODEL`
- 主题：`WEWRITE_THEME`
- 写作（整体只读）：`WEWRITE_WRITER_API_KEY` / `PROVIDER` / `BASE_URL` / `MODEL` / `TEMPERATURE` / `MAX_TOKENS`

### 卷备份

状态全部在命名卷 `wewrite_home`（容器内 `/data/wewrite`）：

```bash
docker-compose run --rm -v wewrite_home:/data -v "$PWD":/backup alpine tar czf /backup/wewrite-home-backup.tgz -C /data .
```

（无 alpine 镜像时可用 `docker cp` 从运行中的 workbench 容器拷贝 `/data/wewrite`。）

## 发布与安全口径

- `permissions.publish` 默认 `false`，只读默认值，不通过全局配置开启任何 run 的发布权限；`mode=publish` 一律 403。
- 预检摘要取值 `state.seo.digest`（手填字段存在时即使空串也优先）→ 否则 `converter.digest`；`len(digest.encode('utf-8')) <= 120`。
- 一次性发布 token：TTL 10 分钟、绑定 run + 标题 + 正文 hash、磁盘只存 hash；撤销授权立即作废未消费 token；`unknown` 状态禁止重复提交；对账只做文本 hash（仅文本核对，不代表图片与排版一致）。
- 无公众号凭据时走 dry-run（真实写入 fail closed），测试用可替换 stub/fake，绝不打真实微信接口。

## 契约基线

`docs/s2-technical-design.md` 是编码基线，任务描述与该设计冲突时以设计第 2/3 节为准。
