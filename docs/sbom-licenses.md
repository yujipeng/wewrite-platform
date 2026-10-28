# SBOM / 运行依赖许可证清单

本文档是 wewrite-platform 运行依赖（不含测试依赖）的软件物料清单（SBOM），用于部署前的许可证合规审查。生成口径见文末「生成与复现」。

## 结论摘要

- 共 **31** 个运行依赖，许可证**均无 GPL / AGPL 强 copyleft 项**，未发现禁用许可。
- 2 个依赖为 **LGPL-3.0-or-later**（弱 copyleft）：`cssutils`、`encutils`，均由 `wewrite==4.2.1` 传递引入，仅以库形式 import、未修改其源码。若目标机许可证政策将 LGPL 也纳入禁用清单，需单独裁决（见「需要裁决项」）。
- 其余 29 个为宽松许可（MIT / BSD / Apache-2.0 / MPL-2.0 / PSF-2.0 / 0BSD / MIT-CMU）。
- 本次 S8-c（JEE-1934）新增 `bcrypt==5.0.0`（Apache-2.0）与 `python-multipart==0.0.32`（Apache-2.0），均为宽松许可，无 copyleft 项。

## 完整清单（按许可证分组）

| 许可证 | 依赖 |
|--------|------|
| 0BSD | chardet |
| Apache-2.0 | opentelemetry-api |
| Apache Software License | requests |
| Apache-2.0 | bcrypt, python-multipart |
| BSD-2-Clause | Pygments |
| BSD-3-Clause | Markdown, click, idna, starlette, uvicorn |
| LGPL-3.0-or-later | cssutils, encutils |
| MIT | annotated-doc, annotated-types, anyio, charset-normalizer, fastapi, more-itertools, pydantic, pydantic_core, soupsieve, typing-inspection, urllib3, wewrite |
| MIT License | PyYAML, beautifulsoup4, h11 |
| MIT-CMU | pillow |
| Mozilla Public License 2.0 (MPL 2.0) | certifi |
| PSF-2.0 | typing_extensions |

## 逐包清单

| Name | Version | License |
|------|---------|---------|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| bcrypt | 5.0.0 | Apache-2.0 |
| beautifulsoup4 | 4.15.0 | MIT License |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) |
| chardet | 7.7.0 | 0BSD |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cssutils | 2.15.0 | LGPL-3.0-or-later |
| encutils | 1.0.0 | LGPL-3.0-or-later |
| fastapi | 0.162.0 | MIT |
| h11 | 0.16.0 | MIT License |
| idna | 3.20 | BSD-3-Clause |
| Markdown | 3.11.2 | BSD-3-Clause |
| more-itertools | 11.2.1 | MIT |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pillow | 13.0.0 | MIT-CMU |
| pydantic | 2.14.1 | MIT |
| pydantic_core | 2.50.1 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT License |
| requests | 2.34.2 | Apache Software License |
| soupsieve | 2.10 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| wewrite | 4.2.1 | MIT |

## 需要裁决项

| 依赖 | 版本 | 许可证 | 说明 |
|------|------|--------|------|
| cssutils | 2.15.0 | LGPL-3.0-or-later | 由 `wewrite==4.2.1` 传递引入；仅以库形式 import，未修改源码 |
| encutils | 1.0.0 | LGPL-3.0-or-later | 由 `cssutils` 传递引入；同上 |

LGPL 属弱 copyleft：以依赖库方式动态/静态链接且不修改其源码时，通常不要求对本项目开源。若目标机许可证政策把 LGPL 一并列入禁用清单，由发起人单独裁决（替换方案：评估 `wewrite` 上游是否可去掉 `cssutils` 依赖）。

## 生成与复现

```bash
# 依赖版本以 requirements.lock 为准（uv pip compile 产物）
python3 -m venv ../.venv-build
../.venv-build/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.lock
../.venv-build/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pip-licenses
../.venv-build/bin/pip-licenses --format=markdown --order=license
```

> 命令输出不含 `pip`、`pip-licenses`、`prettytable`、`wcwidth` 等构建工具自身，仅统计运行依赖。
