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

浏览器打开 `http://127.0.0.1:8765`（Host 必须是 `127.0.0.1:8765` / `localhost:8765` / `[::1]:8765`；机器 IP 或其它域名会被 403 拒绝，除非已列入 `ALLOWED_HOSTS` 白名单，白名单内域名走登录鉴权，见下节「公网域名白名单与登录鉴权」）。

状态根默认 `~/.wewrite`，可用 `WEWRITE_HOME` 覆盖。热榜补抓：`wewrite-platform hotspots refresh [--limit N]`。

### 公网域名白名单与登录鉴权（S8-c）

回环地址（`127.0.0.1` / `localhost` / `[::1]`）保持免登录，本机单机工作流不受影响。经反代暴露公网域名时：

- **域名白名单**：环境变量 `ALLOWED_HOSTS`（逗号分隔，精确匹配），例如 `ALLOWED_HOSTS=temp.os-uat.tcredit.com`。未列入白名单的 Host 一律 403。
- **登录鉴权**：白名单内、非回环的域名访问任意页面/接口时，未登录会 302 跳 `/login`（API 返回 401 `AUTH_REQUIRED`）。`/healthz` 与 `/static/*` 保持免鉴权。
- **账号**：环境变量 `WEWRITE_USERS`，逗号分隔 `username:secret`，例如 `alice:$2b$12$...`。secret **只接受受支持的 bcrypt 哈希**（`$2a$` / `$2b$` / `$2y$` 前缀且可被 bcrypt 解析）；非哈希条目（含明文口令）会被 **fail closed 拒绝**，该账号无法登录。未配置任何账号时登录一律失败（fail closed）。
- **会话**：登录成功签发 HttpOnly + SameSite 会话 cookie（HTTPS 域名自动追加 `Secure`），进程内存保存、重启失效。登出走 `POST /auth/logout` 或访问 `/logout`。
- **CSRF 保留**：白名单域名放行后，变更方法仍强制同源 `Origin`（scheme + host + 端口一致）+ `Sec-Fetch-Site: same-origin` + `X-CSRF-Token`，不回退、不全局关闭。

生成 bcrypt 哈希（供运维配置真实账号）：

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw('你的密码'.encode('utf-8'), bcrypt.gensalt()).decode())"
```

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

打开 `http://127.0.0.1:8765`。健康检查与浏览器验收都用回环地址：容器内监听 `0.0.0.0`，但 compose 只把宿主 `127.0.0.1:8765` 映射进来；用机器 IP 或其它域名访问会被 403 拒绝（除非已列入 `ALLOWED_HOSTS` 白名单，白名单内域名走登录鉴权）。

#### 公网部署（要求说明）

`docker-compose.yml` 的 `workbench.environment` 段已声明 `ALLOWED_HOSTS=${ALLOWED_HOSTS:-}` 与 `WEWRITE_USERS=${WEWRITE_USERS:-}`，docker-compose 会自动读取项目目录下的 `.env` 做插值；`.env` 已列入 `.gitignore`（不入 git、不落镜像层）。

- `WEWRITE_USERS` 只接受受支持的 bcrypt 哈希（`$2a$` / `$2b$` / `$2y$` 前缀且可被 bcrypt 解析），非哈希条目（含明文口令）fail closed 拒绝、该账号无法登录。
- 哈希生成值**自带 `$2b$12$` 前缀**，`.env` 里写 `alice:<完整哈希>` 即可，**替换整个占位符、不要重复保留前缀**（写成 `alice:$2b$12$$2b$12$...` 会被判为非法哈希、该账号 fail closed 无法登录）。
- docker-compose v1.29.2 从 `.env` 插值时 `$2b$12$...` 会**原样传入容器**（`docker-compose config` 显示的 `$$` 只是 YAML 转义展示，容器内 `env` 实测为单 `$`），无需转义 `$`。
- 变量缺省为空时行为 **fail closed**：公网 Host 一律 403、无任何可用账号，回环访问不受影响；这与 `app/auth.py` 的 fail-closed 口径一致。

**具体注入、反代、443 端口映射与部署后校验由 JEE-1914 在 product133 实施**，本仓库只提供变量名与语义，不提供可直接照抄的部署命令；本仓库 compose 只映射宿主回环 `127.0.0.1:8765`。

### 首次配置与密钥注入（env 优先）

密钥**只写不读**：页面写入的 `wechat.secret`、`image.api_key` 只保存在挂载卷 `config.yaml`（0600），不回显明文、不打印日志、不写镜像层。环境变量优先于页面配置，且被环境变量接管的字段写入会返回 `409 ENV_MANAGED`（整包失败不局部提交）。

> 一次性命令（非常驻部署，仅临时跑命令用；默认不映射端口）：

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

### 可复现构建与镜像体积基线

- 依赖**全部锁定**在仓库根 `requirements.lock`（`uv pip compile pyproject.toml -o requirements.lock --python-version 3.11` 产物，含全部传递依赖精确版本）；Dockerfile 先按锁定文件安装、再 `--no-deps` 装本项目，重建镜像的 `pip freeze` 输出完全一致。
- **镜像体积基线**：`wewrite-platform_workbench:latest` 实测 **317 MB**（`python:3.11-slim` + 锁定依赖 + 本项目）。体积回退以该基线判断；若无体积硬约束，此项只记基线不做优化。
- **SBOM / 许可证清单**见 `docs/sbom-licenses.md`：31 个运行依赖无 GPL/AGPL 强 copyleft，2 个 LGPL 依赖（`cssutils`/`encutils`，`wewrite` 传递引入）留裁决项。

## 发布与安全口径

- `permissions.publish` 默认 `false`，只读默认值，不通过全局配置开启任何 run 的发布权限；`mode=publish` 一律 403。
- 预检摘要取值 `state.seo.digest`（手填字段存在时即使空串也优先）→ 否则 `converter.digest`；`len(digest.encode('utf-8')) <= 120`。
- 一次性发布 token：TTL 10 分钟、绑定 run + 标题 + 正文 hash、磁盘只存 hash；撤销授权立即作废未消费 token；`unknown` 状态禁止重复提交；对账只做文本 hash（仅文本核对，不代表图片与排版一致）。
- 无公众号凭据时走 dry-run（真实写入 fail closed），测试用可替换 stub/fake，绝不打真实微信接口。

## 契约基线

`docs/s2-technical-design.md` 是编码基线，任务描述与该设计冲突时以设计第 2/3 节为准。
