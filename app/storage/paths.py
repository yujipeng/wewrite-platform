"""状态根与路径安全层。

按 docs/s2-technical-design.md 第 2/3 节落地，核心不变式：

1. 进程启动时记录 ``$WEWRITE_HOME`` 的 ``st_dev``/``st_ino``；
2. 每次读取以 ``os.open(WEWRITE_HOME, O_DIRECTORY|O_NOFOLLOW)`` 重新取得状态根 fd，
   与启动值比对，不一致即 PATH_FORBIDDEN；
3. 从状态根 fd 起，逐级用 ``dir_fd=`` 打开 ``runs``、``<run_id>``（等价 openat），
   任一级 symlink / 非目录 / ELOOP / ENOTDIR 统一 PATH_FORBIDDEN，绝不回退为按路径打开；
4. 产物文件名走固定白名单，``O_NOFOLLOW|O_RDONLY`` 打开后 ``fstat`` 确认
   ``S_ISREG`` 且 ``st_nlink >= 1``，全程从 fd 读取，读完不再按路径重开。

``safe_open_run_file`` 同时服务 ``GET artifact`` 与 ``POST preview``（M1 建好，M2 复用）。
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from app.domain import errors

# 与上游 wewrite.runs.RUN_ID_RE 保持一致
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")

# 产物白名单（name -> 文件实名），设计第 2 节布局 + 第 3 节 artifact 白名单
_ARTIFACT_FILES = {
    "brief": "brief.yaml",
    "article": "article.md",
    "draft": "draft.md",
    "illustrated_article": "article-illustrated.md",
    "review_report": "review-report.json",
    "images_manifest": "images.json",
    "sources": "sources.yaml",
    "preview": "preview.html",
}

# 设计 2.1 用 images_manifest 对应 images.json，同时保留上游实际文件名别名。
# 白名单只允许以上键，任何路径/扩展名都进不来。
ARTIFACT_NAMES = frozenset(_ARTIFACT_FILES)

_STATE_FILENAME = "state.yaml"

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def validate_run_id(run_id: str) -> str:
    """严格校验 run_id 格式；不合法抛 RUN_ID_INVALID（设计错误码，400）。"""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise errors.run_id_invalid()
    return run_id


@dataclass(frozen=True)
class HomeIdentity:
    """状态根在启动时刻的目录身份。"""

    dev: int
    ino: int


class Paths:
    """状态根路径层：解析 WEWRITE_HOME、记录身份、逐级安全打开。"""

    def __init__(self, home: str | os.PathLike | None = None):
        raw = home if home is not None else os.environ.get("WEWRITE_HOME", "")
        self.home_path = raw if raw else os.path.join(os.path.expanduser("~"), ".wewrite")
        self.home_path = os.path.abspath(self.home_path)
        self._identity = self._capture_identity()

    def _capture_identity(self) -> HomeIdentity | None:
        """记录启动时刻状态根身份；不存在时返回 None（读取时按缺失处理）。"""
        try:
            st = os.stat(self.home_path, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(f"WEWRITE_HOME 不是目录: {self.home_path}")
        return HomeIdentity(dev=st.st_dev, ino=st.st_ino)

    def _open_root(self) -> int:
        """重开状态根目录 fd 并比对启动身份，不一致即 PATH_FORBIDDEN。"""
        if self._identity is None:
            raise errors.path_forbidden({"reason": "enoent", "component": "home"})
        try:
            fd = os.open(self.home_path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            raise errors.path_forbidden({"reason": _os_error_reason(exc), "component": "home"}) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise errors.path_forbidden({"reason": "home_not_directory"})
            if st.st_dev != self._identity.dev or st.st_ino != self._identity.ino:
                raise errors.path_forbidden({"reason": "home_identity_changed"})
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_dir_at(self, parent_fd: int, name: str, what: str) -> int:
        """以 openat 语义逐级打开子目录；symlink/非目录统一 PATH_FORBIDDEN。"""
        try:
            fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise errors.path_forbidden(
                {"reason": _os_error_reason(exc), "component": what}
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise errors.path_forbidden({"reason": "not_directory", "component": what})
        except BaseException:
            os.close(fd)
            raise
        return fd

    def runs_fd(self) -> int:
        """安全打开 runs 目录 fd。若 runs 不存在则视为空列表（返回 -1 哨兵）。"""
        try:
            root_fd = self._open_root()
        except errors.ApiError as exc:
            if exc.details.get("reason") == "enoent" and exc.details.get("component") == "home":
                return -1
            raise
        try:
            try:
                return self._open_dir_at(root_fd, "runs", "runs")
            except errors.ApiError as exc:
                if exc.code == "PATH_FORBIDDEN":
                    # runs 不存在（ENOENT）在列表场景等价于没有 run
                    if exc.details.get("reason") == "enoent":
                        return -1
                raise
        finally:
            os.close(root_fd)

    def run_dir_fd(self, run_id: str) -> int:
        """安全打开 runs/<run_id> 目录 fd；run 不存在抛 NOT_FOUND。"""
        validate_run_id(run_id)
        root_fd = self._open_root()
        try:
            try:
                runs_fd = self._open_dir_at(root_fd, "runs", "runs")
            except errors.ApiError as exc:
                if exc.details.get("reason") == "enoent":
                    raise errors.not_found() from exc
                raise
            try:
                return self._open_dir_at(runs_fd, run_id, "run_id")
            except errors.ApiError as exc:
                if exc.details.get("reason") == "enoent":
                    raise errors.not_found() from exc
                raise
            finally:
                os.close(runs_fd)
        finally:
            os.close(root_fd)


def _os_error_reason(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError) or exc.errno == 2:  # ENOENT
        return "enoent"
    if isinstance(exc, NotADirectoryError) or exc.errno == 20:  # ENOTDIR
        return "enotdir"
    if getattr(exc, "errno", None) == 40:  # ELOOP
        return "eloop"
    if getattr(exc, "errno", None) == 62:  # EMLINK
        return "emlink"
    return "oserror"


def safe_open_run_file(home: str | os.PathLike, run_id: str, name: str):
    """安全打开 run 产物文件，返回 ``(fd, stat_result)``，由调用方 ``os.fdopen`` 读取。

    校验链：run_id 格式 → 状态根身份 → runs → <run_id> → 白名单文件名 →
    O_NOFOLLOW|O_RDONLY → fstat(S_ISREG, st_nlink>=1)。全程不按路径重开。
    """
    if name not in _ARTIFACT_FILES:
        raise errors.not_found({"reason": "artifact_not_whitelisted"})
    validate_run_id(run_id)
    paths = Paths(home)
    try:
        root_fd = paths._open_root()
    except errors.ApiError as exc:
        if exc.details.get("reason") == "enoent" and exc.details.get("component") == "home":
            raise errors.not_found() from exc
        raise
    try:
        try:
            runs_fd = paths._open_dir_at(root_fd, "runs", "runs")
        except errors.ApiError as exc:
            if exc.details.get("reason") == "enoent":
                raise errors.not_found() from exc
            raise
        try:
            try:
                run_fd = paths._open_dir_at(runs_fd, run_id, "run_id")
            except errors.ApiError as exc:
                if exc.details.get("reason") == "enoent":
                    raise errors.not_found() from exc
                raise
            filename = _ARTIFACT_FILES[name]
            try:
                fd = os.open(
                    filename,
                    os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
                    dir_fd=run_fd,
                )
            except OSError as exc:
                raise errors.not_found({"reason": _os_error_reason(exc), "artifact": name}) from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise errors.path_forbidden({"reason": "not_regular_file", "artifact": name})
                if st.st_nlink < 1:
                    raise errors.path_forbidden({"reason": "unlinked_file", "artifact": name})
                return fd, st
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(runs_fd)
    finally:
        os.close(root_fd)


def read_state_yaml(home: str | os.PathLike, run_id: str) -> dict | None:
    """只读投影 state.yaml（不调用上游 load_run，避免升级回写副作用）。"""
    import yaml

    validate_run_id(run_id)
    try:
        fd, _st = safe_open_state(home, run_id)
    except errors.ApiError as exc:
        if exc.code == "NOT_FOUND":
            return None
        raise
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def safe_open_state(home: str | os.PathLike, run_id: str):
    """安全打开 state.yaml（与产物同一套逐级校验，白名单内固定文件名）。"""
    validate_run_id(run_id)
    paths = Paths(home)
    try:
        root_fd = paths._open_root()
    except errors.ApiError as exc:
        if exc.details.get("reason") == "enoent" and exc.details.get("component") == "home":
            raise errors.not_found() from exc
        raise
    try:
        try:
            runs_fd = paths._open_dir_at(root_fd, "runs", "runs")
        except errors.ApiError as exc:
            if exc.details.get("reason") == "enoent":
                raise errors.not_found() from exc
            raise
        try:
            try:
                run_fd = paths._open_dir_at(runs_fd, run_id, "run_id")
            except errors.ApiError as exc:
                if exc.details.get("reason") == "enoent":
                    raise errors.not_found() from exc
                raise
            try:
                fd = os.open(
                    _STATE_FILENAME,
                    os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
                    dir_fd=run_fd,
                )
            except OSError as exc:
                raise errors.not_found({"reason": _os_error_reason(exc)}) from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise errors.path_forbidden({"reason": "not_regular_file"})
                if st.st_nlink < 1:
                    raise errors.path_forbidden({"reason": "unlinked_file"})
                return fd, st
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(runs_fd)
    finally:
        os.close(root_fd)
