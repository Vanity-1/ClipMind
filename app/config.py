"""
ClipMind 知识库系统

核心配置模块 — 支持应用内设置（settings.json 持久化）+ 环境变量回退
"""
import os
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings
from loguru import logger

from app.settings_store import load_settings


def _data_dir() -> str:
    """获取数据目录字符串，供路径拼接使用。"""
    env_dir = os.environ.get("CLIPMIND_DATA_DIR")
    return str(Path(env_dir) if env_dir else Path("data"))


def _default_database_url() -> str:
    """动态计算默认 database_url，避免模块加载时绑定固定 _DATA_DIR。"""
    return f"sqlite+aiosqlite:///{_data_dir()}/bilibili_rag.db"


def _default_chroma_persist_directory() -> str:
    """动态计算默认 chroma_persist_directory。"""
    return f"{_data_dir()}/chroma_db"


# ====== Settings 类「定义单例」机制 ======
# 问题：测试通过 importlib.reload(app.config) 热加载模块，会重新执行类定义语句，
#      生成新的 Settings 类对象（id 不同），导致 `from app.config import Settings` 拿到的
#      是 reload 前的旧类对象，而 _build_settings() 用模块内 Settings 创建的实例
#      __class__ 是新类对象，isinstance(obj, Settings) 返回 False。
#
# 修复：首次定义时把类保存到 sys 模块的自定义属性（sys 不会被 reload），
#      后续 reload 时直接复用该类对象，确保任何时机 import 的 Settings 都是同一个。
_SYS_SINGLETON_ATTR = "_clipmind_settings_class_singleton_v1"

# 1) 先从 sys 中取出已存在的单例（如果是 reload 则不为 None）
_existing_cls = getattr(sys, _SYS_SINGLETON_ATTR, None)


class Settings(BaseSettings):
    """应用配置

    优先级：settings.json > 环境变量 > 默认值
    """

    # Embedding Provider 选择：openai/dashscope/ollama/nvidia，默认 openai
    # 为兼容旧逻辑（按模型前缀路由），留空时走原有的 model_name + base_url 判断
    embedding_provider: str = Field(default="openai", env="EMBEDDING_PROVIDER")

    # OpenAI / LLM 配置
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("DASHSCOPE_API_KEY", "OPENAI_API_KEY"),
    )
    dashscope_api_key: str = Field(
        default="",
        env="DASHSCOPE_API_KEY",
    )
    openai_base_url: str = Field(default="https://api.openai.com/v1", env="OPENAI_BASE_URL")
    llm_model: str = Field(default="gpt-4-turbo", env="LLM_MODEL")
    # LLM Provider 选择：api / ollama，默认 api（保持原有 ChatOpenAI 行为）
    llm_provider: str = Field(default="api", env="LLM_PROVIDER")
    # Ollama 本地模式配置（仅 llm_provider=ollama 时生效）
    ollama_base_url: str = Field(default="http://localhost:11434", env="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="qwen2.5:7b", env="OLLAMA_MODEL")
    embedding_model: str = Field(default="text-embedding-3-small", env="EMBEDDING_MODEL")
    embedding_api_key: str = Field(default="", env="EMBEDDING_API_KEY")
    embedding_base_url: str = Field(default="https://api.openai.com/v1", env="EMBEDDING_BASE_URL")
    chat_use_llm_router: bool = Field(default=False, env="CHAT_USE_LLM_ROUTER")

    # 文本分块配置
    chunk_size: int = Field(default=280, env="CHUNK_SIZE")
    chunk_overlap: int = Field(default=50, env="CHUNK_OVERLAP")

    # DashScope ASR
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1",
        env="DASHSCOPE_BASE_URL",
    )
    # ASR Provider 选择：dashscope/local，默认 local
    asr_provider: str = Field(default="local", env="ASR_PROVIDER")
    # ASR 专用 API Key：留空则直接走本地 faster-whisper，不再复用 LLM 的 openai_api_key。
    # 这样 LLM 用第三方（如 NVIDIA）而 ASR 想用本地 Whisper 时不会被误调 DashScope 导致 401。
    asr_api_key: str = Field(default="", env="ASR_API_KEY")
    asr_model: str = Field(default="paraformer-v2", env="ASR_MODEL")
    asr_timeout: int = Field(default=300, env="ASR_TIMEOUT")
    # 本地 ASR 模型：faster-whisper 模型 size（tiny/base/small/medium/large-v3）
    asr_model_local: str = Field(default="medium", env="ASR_MODEL_LOCAL")
    # local Whisper 独立超时：CPU 上 faster-whisper 较慢，但不应与 DashScope 的 asr_timeout 共用。
    # 默认 300 秒：small/medium 模型在 CPU 上处理 5+ 分钟音频可能需要 2-4 分钟。
    # 配合 beam_size=1 + VAD 滤波 + ffmpeg 预转码 WAV，300s 足够覆盖大多数视频。
    asr_whisper_timeout: int = Field(default=300, env="ASR_WHISPER_TIMEOUT")
    # DashScope Recognition（本地文件直传）使用的模型名，独立于 faster-whisper
    dashscope_recognition_model: str = Field(
        default="paraformer-realtime-v2", env="DASHSCOPE_RECOGNITION_MODEL"
    )
    asr_input_format: str = Field(default="pcm", env="ASR_INPUT_FORMAT")
    # HuggingFace 镜像：faster-whisper 模型下载走国内镜像
    # 默认 hf-mirror.com（社区维护的国内镜像）；留空则走官方 huggingface.co
    hf_mirror_url: str = Field(default="https://hf-mirror.com", env="HF_MIRROR_URL")

    # 应用配置
    app_host: str = Field(default="127.0.0.1", env="APP_HOST")
    app_port: int = Field(default=8000, env="APP_PORT")
    debug: bool = Field(default=False, env="DEBUG")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000,tauri://localhost,http://tauri.localhost,https://tauri.localhost",
        env="CORS_ORIGINS",
    )

    # 数据库
    database_url: str = Field(
        default_factory=_default_database_url,
        env="DATABASE_URL",
    )

    # ChromaDB
    chroma_persist_directory: str = Field(
        default_factory=_default_chroma_persist_directory,
        env="CHROMA_PERSIST_DIRECTORY",
    )

    # Cookie / sensitive field encryption at rest (Fernet key, base64 urlsafe)
    cookie_encryption_key: str = Field(default="", env="COOKIE_ENCRYPTION_KEY")

    # 并发控制
    max_concurrent_ingestion: int = Field(default=5, env="MAX_CONCURRENT_INGESTION")
    # 入库流水线 ASR 阶段并发上限：5 阶段流水线（download/transcode/asr/embedding/done）
    # 中 ASR 受 Semaphore 限流，避免并发 ASR 打爆 GPU/CPU 或上游 API 配额。
    max_asr_concurrency: int = Field(default=2, env="MAX_ASR_CONCURRENCY")

    # Retrieval
    retrieval_candidate_k: int = Field(default=24, env="RETRIEVAL_CANDIDATE_K")
    retrieval_top_k: int = Field(default=8, env="RETRIEVAL_TOP_K")
    retrieval_mmr_fetch_k: int = Field(default=32, env="RETRIEVAL_MMR_FETCH_K")
    retrieval_mmr_lambda: float = Field(default=0.55, env="RETRIEVAL_MMR_LAMBDA")
    # 混合检索开关：True 时 RAGService.search 会同时执行向量召回与 SQLite FTS5 BM25
    # 关键词召回，两路结果用 RRF 融合后返回；False 时回退纯向量检索。
    hybrid_search_enabled: bool = Field(default=True, env="HYBRID_SEARCH_ENABLED")

    # 浏览器池配置
    browser_pool_enabled: bool = Field(default=True, env="BROWSER_POOL_ENABLED")
    browser_pool_max_contexts: int = Field(default=5, env="BROWSER_POOL_MAX_CONTEXTS")
    browser_pool_qrcode_timeout: int = Field(default=10, env="BROWSER_POOL_QRCODE_TIMEOUT")
    browser_pool_qrcode_retries: int = Field(default=2, env="BROWSER_POOL_QRCODE_RETRIES")

    # Langfuse 可观测性（可选，默认关闭）
    langfuse_enabled: bool = Field(default=False, env="LANGFUSE_ENABLED")
    langfuse_public_key: str = Field(default="", env="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", env="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", env="LANGFUSE_HOST")

    # 离线模式 & 应用访问密码
    # offline_mode=True 时拦截外网请求（DashScope/B站/抖音 API），仅允许 localhost
    offline_mode: bool = Field(default=False, env="OFFLINE_MODE")
    # 应用访问密码：空字符串表示无密码保护；非空时所有 API 请求需携带该密码
    auth_password: str = Field(default="", env="AUTH_PASSWORD")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


# 2) Settings 类定义完毕，应用单例替换
#    - 首次定义：_existing_cls is None → 把当前 Settings 类注册到 sys
#    - 模块 reload：_existing_cls 是之前注册的类对象 → 用旧类覆盖当前新定义的类，
#      确保之前 `from app.config import Settings` 拿到的类对象和当前模块的 Settings 是同一个
if _existing_cls is not None:
    Settings = _existing_cls
else:
    setattr(sys, _SYS_SINGLETON_ATTR, Settings)


# 允许 settings.json 用空字符串覆盖默认值的字段。
# 这些字段语义上"留空=显式禁用"，不应被默认值兜底覆盖。
_ALLOW_EMPTY_FIELDS = frozenset({"hf_mirror_url", "auth_password"})


def _build_settings() -> Settings:
    """构建 Settings 实例，用 settings.json 的值覆盖环境变量。"""
    # 先从环境变量 + .env 构建基础实例
    instance = Settings()

    # 再用 settings.json 覆盖（settings.json 优先级最高）
    store = load_settings()
    if store:
        for key, value in store.items():
            if not hasattr(instance, key):
                continue
            if value is None:
                continue
            # 空字符串：仅对白名单字段允许覆盖默认值
            if value == "" and key not in _ALLOW_EMPTY_FIELDS:
                continue
            try:
                setattr(instance, key, value)
            except Exception:
                pass

    return instance


# 全局配置实例
settings = _build_settings()


# ====== 离线模式工具函数 ======
# 离线模式拦截的外网域名列表（DashScope / B站 / 抖音 API）
# 中间件拦截应用层发起的 httpx 请求时，命中此列表的域名会被阻断；
# 仅允许 localhost（Ollama / faster-whisper）等本地服务。
OFFLINE_BLOCKED_DOMAINS = frozenset({
    "dashscope.aliyuncs.com",
    "api.bilibili.com",
    "passport.bilibili.com",
    "www.bilibili.com",
    "api.douyin.com",
    "sso.douyin.com",
    "open.douyin.com",
    "www.douyin.com",
})


class OfflineModeError(RuntimeError):
    """离线模式下发起外网请求时抛出的异常。"""


def is_offline() -> bool:
    """返回当前是否开启离线模式。"""
    return bool(settings.offline_mode)


def require_online(service_name: str = "") -> None:
    """外网调用前检查：若开启离线模式则抛出 OfflineModeError。

    在各外网 service 调用处调用此函数，命中拦截时阻断请求。
    service_name 用于标识发起外网调用的服务，便于日志排查。
    """
    if is_offline():
        label = f"（{service_name}）" if service_name else ""
        raise OfflineModeError(
            f"离线模式已开启，外网请求被拦截{label}"
        )


def is_blocked_domain(url: str) -> bool:
    """判断给定 URL 的域名是否在离线模式拦截列表中。

    用于中间件或 service 层对外网请求域名做精细化拦截。
    localhost / 127.0.0.1 / 0.0.0.0 等本地地址永远不被拦截。
    """
    if not url:
        return False
    # 提取域名：去掉 scheme 后取 host 部分
    try:
        rest = url
        if "://" in rest:
            rest = rest.split("://", 1)[1]
        # 去掉 path / query
        host = rest.split("/", 1)[0].split("?", 1)[0]
        # 去掉端口
        host = host.split(":", 1)[0].lower()
    except Exception:
        return False
    # 本地地址放行
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    return host in OFFLINE_BLOCKED_DOMAINS


def reload_settings() -> Settings:
    """热加载：重新从 settings.json 读取并更新全局实例。

    在 PUT /settings 后调用，使新配置立即生效。

    核心策略：**原地更新**现有 settings 对象，而非替换引用。
    因为 `from app.config import settings` 是值绑定，替换 global settings
    后，已导入的模块仍持有旧对象的引用（如 chat.py / asr.py / crypto.py）。
    原地更新保持对象身份不变，所有引用自动生效。

    如果 database_url 或 debug 发生变化，会重建 database 模块的
    engine 和 async_session_factory，使数据库连接立即指向新配置。
    """
    from app.settings_store import reload_cache
    reload_cache()

    old_db_url = settings.database_url
    old_debug = settings.debug

    # 构建新实例，将其字段逐个复制到现有对象（原地更新）
    new_settings = _build_settings()
    for field_name in type(new_settings).model_fields:
        try:
            setattr(settings, field_name, getattr(new_settings, field_name))
        except Exception:
            pass

    # database_url 或 debug 变化时重建引擎，使新配置立即生效
    if settings.database_url != old_db_url or settings.debug != old_debug:
        try:
            from app.database import rebuild_engine
            rebuild_engine()
            logger.info(
                "数据库引擎已重建"
                f"（database_url: {old_db_url} → {settings.database_url}, "
                f"debug: {old_debug} → {settings.debug}）"
            )
        except Exception as e:
            logger.warning(f"数据库引擎重建失败（非致命，下次重启生效）: {e}")

    # 清理 RAGService 缓存，让下次入库用新配置重建实例
    # 避免改了 embedding 配置后仍用旧实例导致入库失败
    try:
        from app.routers.knowledge import _rag_services
        if _rag_services:
            _rag_services.clear()
            logger.info("RAGService 缓存已清理，下次入库将用新配置重建")
    except Exception as e:
        logger.debug(f"清理 RAGService 缓存失败（非致命）: {e}")

    # 清理 chat.py 的 LLM 客户端单例，使新 API Key / Base URL 立即生效
    # _async_llm_client 是模块级单例，reload 时不清理会导致用旧 Key 发请求
    try:
        import app.routers.chat as _chat_mod
        if _chat_mod._async_llm_client is not None:
            _chat_mod._async_llm_client = None
            logger.info("LLM 客户端单例已清理，下次问答将用新配置创建")
    except Exception as e:
        logger.debug(f"清理 LLM 客户端单例失败（非致命）: {e}")

    logger.info("配置已热加载")
    return settings


def ensure_directories():
    """确保必要的目录存在（全部在数据目录下，打包后可写）"""
    data_dir = _data_dir()
    dirs = [
        data_dir,
        settings.chroma_persist_directory,
        os.path.join(data_dir, "logs"),
        os.path.join(data_dir, "models"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def __getattr__(name: str) -> Any:
    """模块级 __getattr__：动态返回 _DATA_DIR，兼容旧的导入。"""
    if name == "_DATA_DIR":
        return _data_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
