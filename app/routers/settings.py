"""
ClipMind 设置路由

提供 GET /settings（读取，脱敏）、PUT /settings（更新，热加载）、
GET /settings/status（基于上次测试结果）、POST /settings/test（实时测试三类模型）接口。
"""
import asyncio
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel

from app.settings_store import (
    load_settings,
    save_settings,
    mask_sensitive,
    update_last_test_result,
)
from app.config import reload_settings

router = APIRouter(prefix="/settings", tags=["设置"])


class SettingsUpdate(BaseModel):
    """设置更新请求 — 所有字段可选，仅更新传入的字段。"""
    # LLM 配置
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    # LLM 本地模式（Ollama）配置：llm_provider=ollama 时生效
    llm_provider: Optional[str] = None
    ollama_base_url: Optional[str] = None
    ollama_model: Optional[str] = None
    # Embedding Provider 选择：openai/dashscope/ollama/nvidia/local
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None
    chat_use_llm_router: Optional[bool] = None

    # ASR 配置
    # ASR Provider 选择：dashscope/local
    asr_provider: Optional[str] = None
    dashscope_base_url: Optional[str] = None
    asr_api_key: Optional[str] = None
    asr_model: Optional[str] = None
    asr_timeout: Optional[int] = None
    asr_model_local: Optional[str] = None
    dashscope_recognition_model: Optional[str] = None
    asr_input_format: Optional[str] = None
    hf_mirror_url: Optional[str] = None

    # Retrieval 配置
    retrieval_candidate_k: Optional[int] = None
    retrieval_top_k: Optional[int] = None
    retrieval_mmr_fetch_k: Optional[int] = None
    retrieval_mmr_lambda: Optional[float] = None
    # 混合检索开关：True 时向量召回 + SQLite FTS5 BM25 关键词召回 RRF 融合
    hybrid_search_enabled: Optional[bool] = None

    # 入库流水线 ASR 阶段并发上限
    max_asr_concurrency: Optional[int] = None

    # 安全配置
    cookie_encryption_key: Optional[str] = None

    # 离线模式 & 应用访问密码（None 表示不更新）
    offline_mode: Optional[bool] = None
    auth_password: Optional[str] = None


class SettingsTestRequest(BaseModel):
    """设置实时测试请求 — 所有字段可选，空则回退到已保存配置。"""
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_model: Optional[str] = None
    asr_model_local: Optional[str] = None


# 敏感字段：GET /settings 返回脱敏值（含 ****），测试端点不应误用脱敏值
_SENSITIVE_FIELDS = ("openai_api_key", "embedding_api_key", "asr_api_key", "cookie_encryption_key", "auth_password")


def _resolve(payload: SettingsTestRequest, raw: dict[str, Any]) -> dict[str, str]:
    """合并请求字段与已保存配置，请求字段优先。

    空字符串、None、含 **** 的脱敏值均视为未提供，回退到已保存配置。
    这是脱敏值污染的兜底防线：前端 GET /settings 拿到的是脱敏值，
    若误传给测试端点，后端用真实已保存值而非脱敏值请求 API。
    """
    def pick(field: str) -> str:
        val = getattr(payload, field, None)
        if val is None or val == "":
            return str(raw.get(field, "") or "")
        # 敏感字段脱敏值兜底：含 **** 视为未提供，回退到已保存值
        if field in _SENSITIVE_FIELDS and "****" in val:
            return str(raw.get(field, "") or "")
        return val
    return {field: pick(field) for field in (
        "openai_api_key", "openai_base_url", "llm_model",
        "embedding_api_key", "embedding_base_url", "embedding_model",
        "asr_model_local",
    )}


def _merge_last_test_result(category: str, result: dict[str, Any]) -> None:
    """合并测试结果到 last_test_results（并发安全）。

    委托给 settings_store.update_last_test_result，将"读-改-写"全部放在
    threading.Lock 内，保证两个并发调用串行执行，永不丢失字段。
    """
    try:
        update_last_test_result(category, result)
    except Exception as e:
        logger.warning(f"保存 last_test_results 失败: {e}")


def _test_llm(values: dict[str, str]) -> dict[str, Any]:
    """测试 LLM：发一次 max_tokens=1 的 chat completion。"""
    api_key = values["openai_api_key"]
    base_url = values["openai_base_url"]
    model = values["llm_model"]
    if not api_key or not base_url or not model:
        return {"ok": False, "error": "缺少 api_key / base_url / model"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    # LLM 首字节延迟可能较高（agnes-ai 实测 5-12s），用 15s 超时避免误报
    start = time.monotonic()
    try:
        with httpx.Client(timeout=15.0, trust_env=True) as client:
            resp = client.post(url, json=body, headers=headers)
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}", "latency_ms": latency_ms}
        return {"ok": True, "latency_ms": latency_ms}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _test_embedding(values: dict[str, str]) -> dict[str, Any]:
    """测试 Embedding：调一次 embeddings 接口。

    与生产实现保持一致：
    - NVIDIA NIM 模型（model 以 nvidia/ 开头）通过 OpenAI SDK + extra_body 传 input_type
    - base_url 留空时 NVIDIA 模型回退到 https://integrate.api.nvidia.com/v1
    - 代理处理与生产一致（读 HTTPS_PROXY/HTTP_PROXY，trust_env=False）
    """
    import os as _os
    from openai import OpenAI as _OpenAI

    # embedding_api_key 留空时回退到 openai_api_key（沿用项目既有约定）
    api_key = values["embedding_api_key"] or values["openai_api_key"]
    base_url = values["embedding_base_url"] or values["openai_base_url"]
    model = values["embedding_model"]
    if not api_key or not model:
        return {"ok": False, "error": "缺少 api_key / model"}

    # NVIDIA 模型默认 base_url 兜底（与生产 rag.py 一致）
    is_nvidia = model.startswith("nvidia/")
    if not base_url:
        if is_nvidia:
            base_url = "https://integrate.api.nvidia.com/v1"
        else:
            return {"ok": False, "error": "缺少 base_url"}

    # 代理处理与生产 NVIDIAEmbeddings 一致
    proxy = _os.environ.get("HTTPS_PROXY") or _os.environ.get("HTTP_PROXY")
    http_client = httpx.Client(proxy=proxy, trust_env=False) if proxy else None

    start = time.monotonic()
    try:
        client = _OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        # NVIDIA NIM 需要 input_type=query（通过 extra_body 传递，与生产一致）
        extra_body = {"input_type": "query"} if is_nvidia else None
        resp = client.embeddings.create(
            model=model,
            input=["test"],
            extra_body=extra_body,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        # 校验返回数据结构
        if not resp.data or not getattr(resp.data[0], "embedding", None):
            return {"ok": False, "error": "响应缺少 embedding 数据", "latency_ms": latency_ms}
        return {"ok": True, "latency_ms": latency_ms}
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = str(e)
        # 截断过长错误信息
        if len(msg) > 300:
            msg = msg[:300] + "..."
        return {"ok": False, "error": msg, "latency_ms": latency_ms}


def _test_asr_sync(model_size: str) -> dict[str, Any]:
    """同步检测 faster-whisper 模型可加载性（在线程池中执行）。

    测试策略（避免长时间下载阻塞 UI）：
    1. 检查 faster_whisper 模块是否安装
    2. 检查 ctranslate2 后端是否可用（C++ 库能否加载）
    3. 检查本地是否有模型缓存（CLIPMIND_BUNDLED_MODELS_DIR 或 data/models/）
       - 有缓存：触发加载验证（约 3-10s）
       - 无缓存：返回 ok=True, cached=False，提示"首次使用时自动下载"
    这样测试快速且不会因网络问题失败，用户在本地实际使用时才会触发下载。
    """
    # 兜底默认值：settings.json 未存 asr_model_local 时用 medium
    if not model_size:
        model_size = "medium"
    # 1. 检查 faster_whisper 包是否可用
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return {
            "ok": False,
            "error": (
                "未安装 faster-whisper 模块。请下载「带 ASR 模型版」安装包，"
                "或在设置中配置 DashScope API Key 使用云端 ASR。"
            ),
        }
    # 2. 检查 ctranslate2 后端（C++ 库）能否加载
    try:
        import ctranslate2  # noqa: F401
    except ImportError:
        return {
            "ok": False,
            "error": (
                "未安装 ctranslate2 后端（faster-whisper 的 C++ 推理引擎）。"
                "请下载「带 ASR 模型版」安装包。"
            ),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"ctranslate2 后端加载失败（可能动态库缺失）: {e}",
        }
    try:
        from app.services.asr import _get_whisper_model, _resolve_local_model_path
    except Exception as e:
        return {"ok": False, "error": f"asr 模块加载失败: {e}"}
    try:
        # 3. 检查本地模型缓存
        local_path = _resolve_local_model_path(model_size)
        if local_path:
            # 有本地缓存：触发加载验证（约 3-10s）
            _get_whisper_model(model_size)
            return {"ok": True, "cached": True}
        # 无本地缓存：不触发下载（避免长时间阻塞和网络安全问题）
        # 返回 ok=True 表示配置有效，用户首次使用 ASR 时会自动下载
        return {
            "ok": True,
            "cached": False,
            "message": (
                f"faster-whisper 模块可用，本地未缓存 {model_size} 模型。"
                "首次使用 ASR 时会自动下载（medium 约 769MB），或可手动下载放到 data/models/ 目录。"
            ),
        }
    except Exception as e:
        msg = str(e)
        if any(kw in msg.lower() for kw in ("download", "fetch", "local files", "connection", "timeout", "ssl")):
            return {"ok": False, "error": f"模型加载失败（{model_size}），网络不可达: {msg[:120]}"}
        return {"ok": False, "error": msg[:200]}


async def _test_asr(model_size: str) -> dict[str, Any]:
    """异步包装 ASR 测试。

    超时策略：
    - 测试不再触发模型下载（_test_asr_sync 仅检查模块+本地缓存）
    - 本地缓存命中时加载约 3-10s（large 模型可达 30s），60s 超时足够
    - 无缓存时直接返回，不阻塞
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_test_asr_sync, model_size), timeout=60.0
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "检测超时（本地模型加载时间过长，请检查模型文件是否完整）"}


@router.get("")
async def get_settings():
    """获取当前设置（敏感字段脱敏）。"""
    raw = load_settings()
    return mask_sensitive(raw)


@router.put("")
async def update_settings(payload: SettingsUpdate):
    """更新设置并热加载。

    仅写入非 None 字段。空字符串视为"清除该配置"。
    敏感字段含 **** 的脱敏值会被跳过（防止前端误传脱敏值污染配置）。
    """
    updates = {}
    skipped_fields = []
    warnings = []
    for field, value in payload.model_dump(exclude_none=True).items():
        if field in _SENSITIVE_FIELDS:
            # 脱敏值兜底：含 **** 视为未提供，跳过更新
            if isinstance(value, str) and "****" in value:
                skipped_fields.append(field)
                logger.warning(f"敏感字段 {field} 含脱敏值 ****，已跳过更新")
                continue
            # 空字符串保护：允许清除但产生警告
            if value == "":
                warnings.append(f"字段 {field} 已被清空，相关功能将不可用")
        updates[field] = value

    if not updates:
        return {
            "message": "无更新内容" if not skipped_fields else "所有敏感字段均含脱敏值，已跳过",
            "updated": False,
            "skipped_fields": skipped_fields,
            "warnings": warnings,
        }

    save_settings(updates)
    reload_settings()

    logger.info(f"设置已更新: {list(updates.keys())}, 跳过: {skipped_fields}, 警告: {warnings}")
    return {
        "message": "设置已保存并生效",
        "updated": True,
        "fields": list(updates.keys()),
        "skipped_fields": skipped_fields,
        "warnings": warnings,
    }


@router.post("/test")
async def test_settings(payload: SettingsTestRequest):
    """实时测试 LLM / Embedding / ASR 三类配置连通性（兼容旧接口）。

    请求字段为空则回退到 settings.json 中已保存的值。三类测试并行执行，
    测试结果会保存到 settings.json 的 `last_test_results` 字段。
    """
    raw = load_settings()
    values = _resolve(payload, raw)

    llm_result, embedding_result, asr_result = await asyncio.gather(
        asyncio.to_thread(_test_llm, values),
        asyncio.to_thread(_test_embedding, values),
        _test_asr(values["asr_model_local"]),
    )
    results = {"llm": llm_result, "embedding": embedding_result, "asr": asr_result}

    # 持久化到 settings.json（不影响热加载配置，仅记录测试结果）
    try:
        save_settings({"last_test_results": results})
    except Exception as e:
        logger.warning(f"保存 last_test_results 失败: {e}")

    return results


@router.post("/test/llm")
async def test_llm(payload: SettingsTestRequest):
    """单独测试 LLM 配置。"""
    raw = load_settings()
    values = _resolve(payload, raw)
    result = await asyncio.to_thread(_test_llm, values)
    _merge_last_test_result("llm", result)
    return result


@router.post("/test/embedding")
async def test_embedding(payload: SettingsTestRequest):
    """单独测试 Embedding 配置。"""
    raw = load_settings()
    values = _resolve(payload, raw)
    result = await asyncio.to_thread(_test_embedding, values)
    _merge_last_test_result("embedding", result)
    return result


@router.post("/test/asr")
async def test_asr(payload: SettingsTestRequest):
    """单独测试 ASR 本地模型加载。"""
    raw = load_settings()
    values = _resolve(payload, raw)
    result = await _test_asr(values["asr_model_local"])
    _merge_last_test_result("asr", result)
    return result


@router.get("/status")
async def settings_status():
    """检查关键配置是否就绪（基于上次测试结果）。"""
    raw = load_settings()
    last_test = raw.get("last_test_results") or {}
    llm_ok = bool(last_test.get("llm", {}).get("ok"))
    embedding_ok = bool(last_test.get("embedding", {}).get("ok"))
    asr_ok = bool(last_test.get("asr", {}).get("ok"))
    return {
        "llm_configured": llm_ok,
        "embedding_configured": embedding_ok,
        "asr_configured": asr_ok,
        "configured": bool(llm_ok and embedding_ok),
        "tested": bool(last_test),
    }


# Ollama 模型列表接口：单独 router，挂载到 /api/ollama/models
# 供前端在 LLM 本地模式下拉框选择已下载的本地模型
ollama_router = APIRouter(prefix="/api/ollama", tags=["Ollama"])


@ollama_router.get("/models")
async def list_ollama_models():
    """列出本地 Ollama 服务中可用的模型，供前端下拉框使用。

    失败时返回空列表 + 错误信息（不抛异常），前端可降级显示。
    """
    try:
        from ollama import AsyncClient as _OllamaAsyncClient
    except ImportError:
        return {
            "models": [],
            "error": "未安装 ollama 包，请运行: pip install ollama",
        }

    # 优先使用已保存的 ollama_base_url，回退默认本地地址
    raw = load_settings()
    ollama_base_url = (raw.get("ollama_base_url") or "").strip() or "http://localhost:11434"
    try:
        client = _OllamaAsyncClient(host=ollama_base_url)
        # client.list() 返回 ListResponse，其中 .models 为 Model 列表
        resp = await client.list()
        models = []
        # 兼容不同版本 ollama-python 的返回结构
        raw_models = getattr(resp, "models", None)
        if raw_models is None and isinstance(resp, dict):
            raw_models = resp.get("models") or []
        for item in raw_models or []:
            # ollama-python >=0.4: item 为 Model 对象，含 .model / .name
            model_name = getattr(item, "model", None) or getattr(item, "name", None)
            if not model_name and isinstance(item, dict):
                model_name = item.get("model") or item.get("name")
            if model_name:
                models.append(str(model_name))
        logger.info(f"[Ollama] 列出模型成功: {len(models)} 个 (base_url={ollama_base_url})")
        return {"models": models, "error": None}
    except Exception as e:
        msg = str(e)
        if len(msg) > 300:
            msg = msg[:300] + "..."
        logger.warning(f"[Ollama] 列出模型失败 (base_url={ollama_base_url}): {msg}")
        return {"models": [], "error": msg}
