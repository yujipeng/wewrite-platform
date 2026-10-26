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

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime

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
    """状态根目录身份（st_dev / st_ino）。"""

    dev: int
    ino: int


# 进程级状态根身份缓存：key = 状态根绝对路径（os.path.abspath 结果），value = 首次成功
# 打开时钉住的 HomeIdentity。身份只在首次成功打开那一刻固定一次，之后每次读取都以
# `os.open(..., O_DIRECTORY|O_NOFOLLOW)` 重开 fd 并与钉住值比对；不一致即
# 403 PATH_FORBIDDEN。这是设计第 2 节「启动时记录、读取时比对」的进程级落地——不能放在
# 每次请求新建的 Paths 实例里，否则比对永远是自己比自己的 no-op。
#
# 口径（写入注释，防回归）：
#   - 状态根在首次成功打开前不存在 → 列表返回空、单 run 返回 404，不钉身份、不 500；
#   - 状态根首次被 `os.open` 成功打开（且 fstat 为目录）后才把 st_dev/st_ino 钉进缓存；
#   - 钉住之后运行期把状态根 rename 走、原地换成另一个目录 → 重开 fd 的 inode 与钉住值
#     不一致 → 403 PATH_FORBIDDEN（details.reason=home_identity_changed）；
#   - 状态根本身是 symlink → `O_NOFOLLOW` 打开失败（ELOOP）→ 403 PATH_FORBIDDEN。
_home_identity_cache: dict[str, HomeIdentity] = {}


class Paths:
    """状态根路径层：解析 WEWRITE_HOME、逐级安全打开、钉住并比对根身份。"""

    def __init__(self, home: str | os.PathLike | None = None):
        raw = home if home is not None else os.environ.get("WEWRITE_HOME", "")
        self.home_path = raw if raw else os.path.join(os.path.expanduser("~"), ".wewrite")
        self.home_path = os.path.abspath(self.home_path)

    def _open_root(self) -> int:
        """重开状态根目录 fd，并在首次成功打开时钉住身份、之后逐次比对。

        所有打开/取身份失败统一映射为 403 PATH_FORBIDDEN，绝不让 OSError 逃到框架层。
        """
        try:
            fd = os.open(self.home_path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            # 状态根不存在 / 是 symlink(ELOOP) / 是文件(ENOTDIR) 等，统一 PATH_FORBIDDEN
            raise errors.path_forbidden(
                {"reason": _os_error_reason(exc), "component": "home"}
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise errors.path_forbidden({"reason": "home_not_directory", "component": "home"})

            pinned = _home_identity_cache.get(self.home_path)
            if pinned is None:
                # 首次成功打开：钉住身份（进程级缓存，按绝对路径为键）
                _home_identity_cache[self.home_path] = HomeIdentity(
                    dev=st.st_dev, ino=st.st_ino
                )
            elif st.st_dev != pinned.dev or st.st_ino != pinned.ino:
                # 运行期状态根被替换（rename 走再原地换目录）：身份变了，拒绝
                raise errors.path_forbidden(
                    {"reason": "home_identity_changed", "component": "home"}
                )
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

    def hotspots_dir_fd(self, create: bool = False) -> int:
        """安全打开 ``hotspots`` 目录 fd；不存在时（create=False）返回 -1 哨兵。

        与 ``runs/`` 同一套逐级 openat + O_NOFOLLOW：从已钉住身份的根 fd 起
        打开 ``hotspots``，任一级 symlink/非目录统一 PATH_FORBIDDEN，不回退按路径打开。
        ``create=True`` 仅在写路径用：先在根 fd 下相对 ``mkdir("hotspots")``，再重开。
        """
        try:
            root_fd = self._open_root()
        except errors.ApiError as exc:
            if exc.details.get("reason") == "enoent" and exc.details.get("component") == "home":
                return -1
            raise
        try:
            try:
                return self._open_dir_at(root_fd, "hotspots", "hotspots")
            except errors.ApiError as exc:
                if exc.code == "PATH_FORBIDDEN" and exc.details.get("reason") == "enoent":
                    if not create:
                        return -1
                    # 在已打开的根 fd 下相对创建（不按绝对路径拼接），再 O_NOFOLLOW 重开。
                    try:
                        os.mkdir("hotspots", dir_fd=root_fd)
                    except FileExistsError:
                        pass
                    except OSError as e:
                        raise errors.path_forbidden(
                            {"reason": _os_error_reason(e), "component": "hotspots"}
                        ) from e
                    return self._open_dir_at(root_fd, "hotspots", "hotspots")
                raise
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


def sha256_hex(data: bytes) -> str:
    """内容 SHA-256（预览 source_sha256 等复用）。"""
    return hashlib.sha256(data).hexdigest()


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


def _validate_hotspot_date(date: str) -> str:
    """热榜快照日期格式校验（YYYY-MM-DD）。"""
    import re as _re

    if not isinstance(date, str) or not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise errors.validation_error("date 必须为 YYYY-MM-DD 格式", {"field": "date"})
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise errors.validation_error("date 不是合法日期", {"field": "date"})
    return date


def safe_open_hotspot_file(home: str | os.PathLike, date: str):
    """安全打开 ``hotspots/<date>.json``，返回 ``(fd, stat_result)``。

    与 ``safe_open_run_file`` 同一套逐级 openat：状态根身份 → hotspots 目录 →
    ``<date>.json``（O_NOFOLLOW|O_RDONLY）→ fstat(S_ISREG, st_nlink>=1)。
    目录或文件缺失返回 NOT_FOUND；任一级 symlink/非目录统一 PATH_FORBIDDEN。
    """
    _validate_hotspot_date(date)
    paths = Paths(home)
    try:
        root_fd = paths._open_root()
    except errors.ApiError as exc:
        if exc.details.get("reason") == "enoent" and exc.details.get("component") == "home":
            raise errors.not_found() from exc
        raise
    try:
        try:
            hotspots_fd = paths._open_dir_at(root_fd, "hotspots", "hotspots")
        except errors.ApiError as exc:
            if exc.details.get("reason") == "enoent":
                raise errors.not_found() from exc
            raise
        try:
            try:
                fd = os.open(
                    f"{date}.json",
                    os.O_RDONLY | _O_NOFOLLOW | _O_BINARY,
                    dir_fd=hotspots_fd,
                )
            except OSError as exc:
                reason = _os_error_reason(exc)
                if reason == "eloop":
                    raise errors.path_forbidden({"reason": "eloop", "date": date}) from exc
                raise errors.not_found({"reason": reason, "date": date}) from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise errors.path_forbidden({"reason": "not_regular_file", "date": date})
                if st.st_nlink < 1:
                    raise errors.path_forbidden({"reason": "unlinked_file", "date": date})
                return fd, st
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(hotspots_fd)
    finally:
        os.close(root_fd)


def list_hotspot_filenames(home: str | os.PathLike) -> list[str]:
    """安全枚举 ``hotspots/`` 下的快照文件名（``<date>.json``），目录不存在返回空列表。

    只做「安全打开目录 + 枚举合法日期名」，保留期过滤由调用方（store）负责。
    """
    paths = Paths(home)
    hotspots_fd = paths.hotspots_dir_fd(create=False)
    if hotspots_fd == -1:
        return []
    names = []
    try:
        with os.scandir(hotspots_fd) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                d = entry.name[:-5]
                try:
                    _validate_hotspot_date(d)
                except errors.ApiError:
                    continue
                names.append(d)
    finally:
        os.close(hotspots_fd)
    return names


def safe_write_hotspot_file(home: str | os.PathLike, date: str, text: str) -> None:
    """安全原子写 ``hotspots/<date>.json``。

    从已打开的根 fd 起安全打开/创建 hotspots 目录，临时文件与目标都在该目录 fd 下
    （``O_NOFOLLOW`` + ``dir_fd``），写入 fsync 后 ``os.replace`` 再 fsync 目录，
    全程不按绝对路径拼接后 open。任一级 symlink/非目录 → PATH_FORBIDDEN。
    """
    _validate_hotspot_date(date)
    paths = Paths(home)
    root_fd = paths._open_root()
    try:
        try:
            hotspots_fd = paths._open_dir_at(root_fd, "hotspots", "hotspots")
        except errors.ApiError as exc:
            if exc.code == "PATH_FORBIDDEN" and exc.details.get("reason") == "enoent":
                try:
                    os.mkdir("hotspots", dir_fd=root_fd)
                except FileExistsError:
                    pass
                except OSError as e:
                    raise errors.path_forbidden(
                        {"reason": _os_error_reason(e), "component": "hotspots"}
                    ) from e
                hotspots_fd = paths._open_dir_at(root_fd, "hotspots", "hotspots")
            else:
                raise
        try:
            # 同目录临时文件：O_CREAT|O_EXCL 保证唯一，权限 0600
            import secrets as _secrets

            tmp_name = f".{date}.{_secrets.token_hex(6)}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                    0o600,
                    dir_fd=hotspots_fd,
                )
            except OSError as exc:
                raise errors.path_forbidden(
                    {"reason": _os_error_reason(exc), "component": "hotspots"}
                ) from exc
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(
                    tmp_name,
                    f"{date}.json",
                    src_dir_fd=hotspots_fd,
                    dst_dir_fd=hotspots_fd,
                )
                os.fsync(hotspots_fd)
            except BaseException:
                try:
                    os.unlink(tmp_name, dir_fd=hotspots_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(hotspots_fd)
    finally:
        os.close(root_fd)
