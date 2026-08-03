"""
ClipMind 设置持久化模块

将所有配置项持久化到 data/settings.json，支持运行时热加载。
替代原有的 .env 配置方式，实现"应用内设置"。

数据目录支持环境变量 CLIPMIND_DATA_DIR 覆盖（桌面应用打包后指向用户 AppData）。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from loguru import logger


def _get_data_dir() -> Path:
    """确定数据目录，支持环境变量覆盖。

    优先级：CLIPMIND_DATA_DIR 环境变量 > 当前目录下的 data/

    **注意**：每次调用都会重新读取环境变量，确保测试设置 CLIPMIND_DATA_DIR
    后立即生效，不依赖模块加载顺序或 importlib.reload。
    """
    env_dir = os.environ.get("CLIPMIND_DATA_DIR")
    if env_dir:
        path = Path(env_dir)
    else:
        path = Path("data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_settings_dir() -> Path:
    """返回 settings.json 所在目录。每次调用动态计算。"""
    return _get_data_dir()


def _get_settings_file() -> Path:
    """返回 settings.json 的完整路径。每次调用动态计算。"""
    return _get_settings_dir() / "settings.json"


# 线程锁，保证并发读写安全
_lock = threading.Lock()

# 内存中的设置缓存（注意：变量名 _cache 被测试 fixture 显式清理，不能改！）
_cache: Optional[dict[str, Any]] = None
_cache_path: Optional[str] = None          # settings.json 绝对路径字符串（os.fspath）
_cache_mtime: Optional[int] = None         # 文件修改时间（秒级）
_cache_size: Optional[int] = None          # 文件大小
_settings_file_gen: int = 0                # 文件世代：每次 _ensure_settings_file 新建文件时递增，用于 fixture 只清 _cache 没清路径时的缓存失效
_cache_saved_gen: int = -1                 # 缓存对应的文件世代号，不匹配则缓存失效
_cached_env_dir: Optional[str] = None      # 缓存时对应的 CLIPMIND_DATA_DIR 环境变量值，变化则强制失效

# 敏感字段列表（GET 时脱敏）
_SENSITIVE_KEYS = {
    "openai_api_key",
    "embedding_api_key",
    "asr_api_key",
    "cookie_encryption_key",
    "auth_password",
}


def _ensure_settings_file() -> bool:
    """确保设置文件和目录存在。返回 True 表示新建了文件（需要递增 gen 使缓存失效）。"""
    global _settings_file_gen
    settings_dir = _get_settings_dir()
    settings_file = _get_settings_file()
    settings_dir.mkdir(parents=True, exist_ok=True)
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps({}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _settings_file_gen += 1
        return True
    return False


def load_settings() -> dict[str, Any]:
    """从磁盘加载设置，带内存缓存（变量名 _cache 兼容 fixture 显式清理）。"""
    global _cache, _cache_path, _cache_mtime, _cache_size, _cache_saved_gen, _cached_env_dir
    with _lock:
        settings_file = _get_settings_file()
        path_str = os.fspath(settings_file.resolve())
        curr_env_dir = os.environ.get("CLIPMIND_DATA_DIR") or ""

        # 检查缓存有效性：_cache非None + 路径匹配 + 文件世代匹配 + 环境变量匹配
        cache_valid = (
            _cache is not None
            and _cache_path == path_str
            and _cache_saved_gen == _settings_file_gen
            and _cached_env_dir == curr_env_dir
        )
        # 额外检查文件 mtime/size，检测外部修改（如直接编辑 settings.json）
        if cache_valid:
            try:
                stat = settings_file.stat()
                if int(stat.st_mtime) != _cache_mtime or stat.st_size != _cache_size:
                    cache_valid = False
            except OSError:
                cache_valid = False
        if cache_valid:
            return dict(_cache)

        _ensure_settings_file()
        try:
            raw = settings_file.read_text(encoding="utf-8")
            _cache = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取设置文件失败，使用空配置: {e}")
            _cache = {}

        # 更新缓存校验元数据
        _cache_path = path_str
        _cache_saved_gen = _settings_file_gen
        _cached_env_dir = curr_env_dir
        try:
            stat = settings_file.stat()
            _cache_mtime = int(stat.st_mtime)
            _cache_size = stat.st_size
        except OSError:
            pass

        return dict(_cache)


def save_settings(data: dict[str, Any]) -> None:
    """将设置写入磁盘并更新缓存。"""
    global _cache, _cache_path, _cache_mtime, _cache_size, _cache_saved_gen, _cached_env_dir
    with _lock:
        _ensure_settings_file()
        settings_file = _get_settings_file()
        path_str = os.fspath(settings_file.resolve())
        curr_env_dir = os.environ.get("CLIPMIND_DATA_DIR") or ""

        # 先读取当前磁盘内容（如果缓存无效），再合并新值
        cache_valid = (
            _cache is not None
            and _cache_path == path_str
            and _cache_saved_gen == _settings_file_gen
            and _cached_env_dir == curr_env_dir
        )
        need_read_disk = not cache_valid
        if need_read_disk:
            try:
                raw = settings_file.read_text(encoding="utf-8")
                current = json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, OSError):
                current = {}
        else:
            current = dict(_cache)

        current.update(data)

        # 写磁盘
        settings_file.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新缓存 + 校验元数据
        _cache = dict(current)
        _cache_path = path_str
        _cache_saved_gen = _settings_file_gen
        _cached_env_dir = curr_env_dir
        try:
            stat = settings_file.stat()
            _cache_mtime = int(stat.st_mtime)
            _cache_size = stat.st_size
        except OSError:
            pass

        logger.info("设置已保存到 data/settings.json")


def update_last_test_result(category: str, result: Any) -> None:
    """原子地合并单个测试结果到 last_test_results（并发安全）。"""
    global _cache, _cache_path, _cache_mtime, _cache_size, _cache_saved_gen, _cached_env_dir
    with _lock:
        _ensure_settings_file()
        settings_file = _get_settings_file()
        path_str = os.fspath(settings_file.resolve())
        curr_env_dir = os.environ.get("CLIPMIND_DATA_DIR") or ""

        # 读取当前内容（缓存有效则用缓存）
        cache_valid = (
            _cache is not None
            and _cache_path == path_str
            and _cache_saved_gen == _settings_file_gen
            and _cached_env_dir == curr_env_dir
        )
        need_read_disk = not cache_valid
        if need_read_disk:
            try:
                raw = settings_file.read_text(encoding="utf-8")
                current = json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, OSError):
                current = {}
        else:
            current = dict(_cache)

        last = current.get("last_test_results") or {}
        last = dict(last)
        last[category] = result
        current["last_test_results"] = last

        # 写磁盘
        settings_file.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 更新缓存 + 校验元数据
        _cache = dict(current)
        _cache_path = path_str
        _cache_saved_gen = _settings_file_gen
        _cached_env_dir = curr_env_dir
        try:
            stat = settings_file.stat()
            _cache_mtime = int(stat.st_mtime)
            _cache_size = stat.st_size
        except OSError:
            pass

        logger.info(f"last_test_results.{category} 已更新")


def get_setting(key: str, default: Any = None) -> Any:
    """获取单个设置项。"""
    settings = load_settings()
    return settings.get(key, default)


def mask_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """脱敏敏感字段，用于 GET /settings 响应。"""
    masked = dict(data)
    for key in _SENSITIVE_KEYS:
        val = masked.get(key)
        if val and isinstance(val, str) and len(val) > 8:
            masked[key] = val[:4] + "*" * (len(val) - 8) + val[-4:]
        elif val:
            masked[key] = "****"
    return masked


def reload_cache() -> None:
    """强制重新从磁盘加载设置（热加载时调用）。"""
    global _cache, _cache_path, _cache_mtime, _cache_size, _cache_saved_gen, _cached_env_dir
    with _lock:
        _cache = None
        _cache_path = None
        _cache_mtime = None
        _cache_size = None
        _cache_saved_gen = -1
        _cached_env_dir = None
    load_settings()
