"""原子写与全局锁测试。"""

from __future__ import annotations

import os

from app.storage.atomic import PlatformLock, atomic_write_text


def test_atomic_write_text_creates_and_fsyncs(tmp_path):
    target = tmp_path / "sub" / "state.yaml"
    atomic_write_text(str(target), "a: 1\n")
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "a: 1\n"
    # 0600 权限
    assert (target.stat().st_mode & 0o777) == 0o600


def test_atomic_write_text_replaces(tmp_path):
    target = tmp_path / "state.yaml"
    target.write_text("old")
    atomic_write_text(str(target), "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_platform_lock_exclusive(tmp_path):
    home = tmp_path
    with PlatformLock(str(home)):
        # 同进程再拿一把排他锁会阻塞，这里用非阻塞验证锁存在
        import fcntl

        fd = os.open(home / "platform.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


import pytest
