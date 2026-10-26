# 测试环境与运行口径（唯一推荐装法）

本文件是 wewrite-platform 全量测试的**唯一可复现运行口径**。任何跑测试的智能体或人，都必须按这里装，不要再各装各的。

## 为什么需要这份口径

- 系统 `python3` 不含项目依赖（`fastapi` 等），直接 `python3 -m pytest` 会 `ModuleNotFoundError`，全量测试 exit 4。
- 直连 pypi.org 极慢（实测单文件约 11–16 KB/s），`pip install -e ".[test]"` 十分钟仍停在下载阶段。
- venv 建在 checkout 内会污染 `git status`，交付 / 合并时容易误带未跟踪目录。

## 唯一推荐装法（硬要求）

```bash
# 1. 在 checkout 之外建 venv（不要在仓库目录内）
python3 -m venv ../.venv-test

# 2. 用清华镜像安装（含 test extra）
../.venv-test/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[test]"

# 3. 跑全量测试
../.venv-test/bin/python -m pytest -q
```

三条硬要求：

1. **venv 必须建在 checkout 之外**（例如仓库上一级目录）。
2. **必须用清华镜像**：`-i https://pypi.tuna.tsinghua.edu.cn/simple`。
3. **必须用 `-e ".[test]"` 安装**，并只用该 venv 里的 `python` 跑测试，不要用系统 `python3`。

预期结果：`96 passed`（以当前 main 的测试集为准）。

## 常见报错对照

| 现象 | 原因 | 处理 |
|------|------|------|
| `ModuleNotFoundError: No module named 'fastapi'` | 用了系统 `python3` 或 venv 未装依赖 | 按上面唯一装法重建 venv |
| pip 下载长期停在某个文件 | 直连 pypi.org | 补上清华镜像 |
| `git status` 出现 `.venv/` 未跟踪目录 | venv 建在了仓库内 | 删除后改到 checkout 外重建 |

## 依赖说明

- `pytest`、`httpx` 来自 `pyproject.toml` 的 `[project.optional-dependencies] test`。
- `bs4` / `lxml` 等由 `wewrite==4.2.1` 传递依赖带入，**不是**缺声明，无需额外安装。
- 如需钉住测试依赖版本，可另建 `requirements-test.txt` 或 `constraints.txt`（当前未做，按需补充）。
