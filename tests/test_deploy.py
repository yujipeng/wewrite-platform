"""M4 容器化 smoke：compose 文件语法校验（本机只有 docker-compose v1.29.2）。"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_dockerfile_and_compose_exist():
    assert os.path.exists(os.path.join(REPO, "Dockerfile"))
    assert os.path.exists(os.path.join(REPO, "docker-compose.yml"))


def test_dockerfile_non_root_and_no_secret():
    dockerfile = open(os.path.join(REPO, "Dockerfile"), encoding="utf-8").read()
    assert "USER wewrite" in dockerfile
    assert "python:3.11-slim" in dockerfile
    # 镜像内不落密钥（不出现示例 secret）
    assert "WECHAT_SECRET=" not in dockerfile


@pytest.mark.skipif(shutil.which("docker-compose") is None, reason="无 docker-compose")
def test_compose_config_valid():
    proc = subprocess.run(
        ["docker-compose", "-f", "docker-compose.yml", "config"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    # 端口只映射 loopback
    assert "127.0.0.1:8765:8765" in proc.stdout
    assert "wewrite_home" in proc.stdout
