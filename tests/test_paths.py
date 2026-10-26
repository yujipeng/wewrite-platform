"""路径安全层测试：路径穿越、URL 编码、绝对路径、叶文件 symlink、非法 run_id、
祖先目录 symlink 替换（runs/ 与 runs/<run_id>/ 均 403 且读不到外部同名文件）。"""

from __future__ import annotations

import os
import threading

import pytest

from app.domain import errors
from app.storage import paths


def test_validate_run_id_accepts_legal():
    assert paths.validate_run_id("20261026-080000-a1b2c3") == "20261026-080000-a1b2c3"


@pytest.mark.parametrize(
    "bad",
    [
        "../secrets",
        "20261026-080000-a1b2c3/../../etc/passwd",
        "/etc/passwd",
        "20261026-080000-A1B2C3",  # 大写 hex 不合法
        "20261026-080000-a1b2c",   # 短一位
        "2026-10-26-08-00-00-abc",  # 分隔符错误
        "20261026-080000-a1b2c3x",  # 超长
        "",
    ],
)
def test_validate_run_id_rejects(bad):
    with pytest.raises(errors.ApiError) as ei:
        paths.validate_run_id(bad)
    assert ei.value.code == "RUN_ID_INVALID"


def test_safe_open_run_file_rejects_non_whitelisted(three_state_home):
    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(three_state_home, "20261026-080000-a1b2c3", "config.yaml")
    assert ei.value.code == "NOT_FOUND"


def test_safe_open_run_file_reads_whitelisted(three_state_home):
    fd, st = paths.safe_open_run_file(three_state_home, "20261026-100000-abcdef", "article")
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        content = f.read()
    assert "article" in content


def test_leaf_symlink_rejected(three_state_home):
    """叶文件换成 symlink：必须 403 且不读外部同名文件。"""
    run_dir = os.path.join(three_state_home, "runs", "20261026-100000-abcdef")
    outside = os.path.join(three_state_home, "outside-article.md")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("OUTSIDE SECRET")
    target = os.path.join(run_dir, "article.md")
    os.remove(target)
    os.symlink(outside, target)

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(three_state_home, "20261026-100000-abcdef", "article")
    assert ei.value.code in {"PATH_FORBIDDEN", "NOT_FOUND"}


def test_runs_symlink_rejected(three_state_home):
    """runs/ 被替换为 symlink：403 且读不到外部同名文件。"""
    runs = os.path.join(three_state_home, "runs")
    os.rename(runs, os.path.join(three_state_home, "runs.real"))
    outside = os.path.join(three_state_home, "evil")
    os.makedirs(os.path.join(outside, "20261026-100000-abcdef"), exist_ok=True)
    with open(os.path.join(outside, "20261026-100000-abcdef", "article.md"), "w", encoding="utf-8") as f:
        f.write("EVIL CONTENT")
    os.symlink(outside, runs)

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(three_state_home, "20261026-100000-abcdef", "article")
    assert ei.value.code == "PATH_FORBIDDEN"


def test_run_dir_symlink_rejected(three_state_home):
    """runs/<run_id>/ 被替换为 symlink：403 且读不到外部同名文件。"""
    run_dir = os.path.join(three_state_home, "runs", "20261026-100000-abcdef")
    os.rename(run_dir, os.path.join(three_state_home, "runs", "real-20261026-100000-abcdef"))
    outside = os.path.join(three_state_home, "evil-run")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "article.md"), "w", encoding="utf-8") as f:
        f.write("EVIL RUN CONTENT")
    os.symlink(outside, run_dir)

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(three_state_home, "20261026-100000-abcdef", "article")
    assert ei.value.code == "PATH_FORBIDDEN"


def test_concurrent_swap_reads_opened_fd_snapshot(three_state_home):
    """并发换 symlink 时，读取走已打开 fd 快照（不是被替换后的路径）。"""
    run_id = "20261026-100000-abcdef"
    run_dir = os.path.join(three_state_home, "runs", run_id)
    original = os.path.join(run_dir, "article.md")
    with open(original, "w", encoding="utf-8") as f:
        f.write("ORIGINAL CONTENT")

    # 先拿到已打开的 fd（模拟请求已进入 safe_open_run_file 之后、读取之前）
    fd, _st = paths.safe_open_run_file(three_state_home, run_id, "article")

    # 打开后替换目录为 symlink 指向外部目录（恶意并发替换）
    evil = os.path.join(three_state_home, "evil-concurrent")
    os.makedirs(evil, exist_ok=True)
    with open(os.path.join(evil, "article.md"), "w", encoding="utf-8") as f:
        f.write("EVIL CONCURRENT CONTENT")
    os.rename(run_dir, os.path.join(three_state_home, "runs", "real-concurrent"))
    os.symlink(evil, run_dir)

    # 从已打开的 fd 读取，必须是打开时刻的快照内容
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        content = f.read()
    assert "ORIGINAL" in content
    assert "EVIL" not in content


def test_url_encoded_run_id_rejected_api(client):
    """URL 编码的穿越在路由层即被拒绝。"""
    r = client.get("/api/v1/runs/%2e%2e%2fsecrets")
    assert r.status_code in {400, 404}


def test_absolute_path_run_id_rejected_api(client):
    r = client.get("/api/v1/runs//etc/passwd")
    assert r.status_code in {400, 404}


def test_runtime_home_replacement_rejected_safe_open(three_state_home):
    """阻断 A：进程级身份钉住后，运行期把 WEWRITE_HOME 换成另一个真实目录，
    safe_open_run_file 必须 403 PATH_FORBIDDEN 且读不到替换目录内容。"""
    run_id = "20261026-100000-abcdef"
    # 首次打开：钉住身份
    fd, _st = paths.safe_open_run_file(three_state_home, run_id, "article")
    os.close(fd)

    # 运行期 rename 走原目录，原地换成一个含同名 run + 恶意 article.md 的真实目录
    backup = three_state_home + ".bak"
    os.rename(three_state_home, backup)
    os.makedirs(os.path.join(three_state_home, "runs", run_id), exist_ok=True)
    with open(os.path.join(three_state_home, "runs", run_id, "article.md"), "w", encoding="utf-8") as f:
        f.write("EVIL-ARTICLE-CONTENT")

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(three_state_home, run_id, "article")
    assert ei.value.code == "PATH_FORBIDDEN"
    assert ei.value.details.get("reason") == "home_identity_changed"


def test_runtime_home_replacement_rejected_api(client, three_state_home):
    """阻断 A：GET /api/v1/runs 在状态根被替换后必须 403，且不列出替换目录里的 run。"""
    # 首次读取：钉住身份
    r = client.get("/api/v1/runs")
    assert r.status_code == 200

    # 运行期替换状态根
    backup = three_state_home + ".bak"
    os.rename(three_state_home, backup)
    os.makedirs(os.path.join(three_state_home, "runs", "20261026-999999-abcdef"), exist_ok=True)
    with open(os.path.join(three_state_home, "runs", "20261026-999999-abcdef", "state.yaml"), "w", encoding="utf-8") as f:
        f.write("run_id: 20261026-999999-abcdef\nstatus: active\n")

    r = client.get("/api/v1/runs")
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"
    assert r.json()["details"]["reason"] == "home_identity_changed"


def test_symlink_home_rejected(tmp_path):
    """阻断 B：WEWRITE_HOME 本身是 symlink 时返回 403 PATH_FORBIDDEN，不得 500。"""
    real = tmp_path / "real-home"
    real.mkdir()
    os.makedirs(real / "runs", exist_ok=True)
    link = tmp_path / "wewrite-link"
    os.symlink(str(real), str(link))

    with pytest.raises(errors.ApiError) as ei:
        paths.safe_open_run_file(str(link), "20261026-100000-abcdef", "article")
    assert ei.value.code == "PATH_FORBIDDEN"
    assert ei.value.details.get("reason") in {"eloop", "enotdir"}


def test_symlink_home_rejected_api(tmp_path, monkeypatch):
    """阻断 B：symlink 状态根经 API 请求返回 403，不得 500。"""
    real = tmp_path / "real-home"
    real.mkdir()
    os.makedirs(real / "runs", exist_ok=True)
    link = tmp_path / "wewrite-link"
    os.symlink(str(real), str(link))

    monkeypatch.setenv("WEWRITE_HOME", str(link))
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.get("/api/v1/runs", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 403
    assert r.json()["code"] == "PATH_FORBIDDEN"


def test_empty_home_returns_empty_list_not_500(tmp_path, monkeypatch):
    """空 WEWRITE_HOME（不存在）仍应返回空列表，不得 500（设计口径）。"""
    missing = str(tmp_path / "not-yet-created")
    monkeypatch.setenv("WEWRITE_HOME", missing)
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.get("/api/v1/runs", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 200
    assert r.json()["runs"] == []

    # 单 run 详情：缺失 → 404 而非 500
    r = c.get("/api/v1/runs/20261026-100000-abcdef", headers={"host": "127.0.0.1:8765"})
    assert r.status_code == 404
