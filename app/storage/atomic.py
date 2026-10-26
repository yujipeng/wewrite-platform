"""全局锁与原子写。

设计第 2 节：``$WEWRITE_HOME/platform.lock`` 用 ``fcntl.flock`` 做全局写锁；
JSON/YAML 写入固定为「同目录临时文件 0600 → UTF-8 写入 → fsync → os.replace →
父目录 fsync」。上游 wewrite.runs 自身不加锁，锁由平台实现。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path


class PlatformLock:
    """持有平台全局写锁的上下文管理器。"""

    def __init__(self, home: str | os.PathLike):
        self.lock_path = Path(home) / "platform.lock"
        self._fd: int | None = None

    def __enter__(self) -> "PlatformLock":
        # 不预先创建 WEWRITE_HOME：读路径不应触发写；写路径在加锁前显式 ensure。
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def atomic_write_text(path: str | os.PathLike, text: str, mode: int = 0o600) -> None:
    """同目录临时文件写入 → fsync → os.replace → 父目录 fsync。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str | os.PathLike, data: bytes, mode: int = 0o600) -> None:
    """二进制原子写（M2 之后的产物落盘复用）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
