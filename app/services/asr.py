"""
Bilibili RAG 知识库系统

ASR 服务 - 使用 DashScope 录音文件识别
"""
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from http import HTTPStatus
from typing import Optional, Any
from urllib import request as urlrequest

import httpx
import dashscope
from loguru import logger
from app.services.tracing import trace_logger

from app.config import settings
from app.services.cancellation import CancelCheck, ensure_not_cancelled
from app.services.url_safety import is_safe_dashscope_url


# 延迟导入 dashscope 子模块的工具函数。
# 这些子模块在导入时会拉起较重的依赖链，放顶层会让后端启动变慢且增加
# PyInstaller 漏收风险；改成首次调用时按需加载即可。
def _join_url(base_url: str, *parts: str) -> str:
    from dashscope.common.utils import join_url
    return join_url(base_url, *parts)


def _default_headers(api_key: str) -> dict:
    from dashscope.common.utils import default_headers
    return default_headers(api_key)

# --- Whisper local ASR fallback ---
# 按模型名缓存，用户切换 asr_model_local 后能加载新模型
_WHISPER_MODELS: dict[str, "WhisperModel"] = {}
_WHISPER_MODEL_LOCK = threading.Lock()

_VALID_WHISPER_SIZES = {
    "tiny", "base", "small", "medium",
    "large-v1", "large-v2", "large-v3",
}


def _normalize_windows_path(path: str) -> str:
    """剥离 Windows 扩展长度路径前缀 \\\\?\\ 和 \\\\?\\UNC\\。

    Tauri 的 resource_dir() 在某些 Windows 环境下返回带 \\\\?\\ 前缀的路径，
    而 faster-whisper/ctranslate2 的 C++ 端（std::ifstream）无法可靠处理
    这种前缀，导致模型加载失败。Python 端 os.path.isfile 能正常处理该前缀，
    所以 _is_valid_model_dir 校验通过，但 WhisperModel(local_path) 加载失败。
    """
    if os.name == "nt":
        if path.startswith("\\\\?\\UNC\\"):
            return "\\\\" + path[len("\\\\?\\UNC\\"):]
        if path.startswith("\\\\?\\"):
            return path[len("\\\\?\\"):]
    return path


def _apply_hf_mirror() -> str:
    """应用 HuggingFace 镜像配置。

    faster-whisper 通过 huggingface_hub.snapshot_download 下载模型，
    走 HF_ENDPOINT 环境变量。huggingface_hub 在首次 import 时把 HF_ENDPOINT
    缓存到 constants.ENDPOINT 常量，因此需要双管齐下：
      1. 设置 os.environ["HF_ENDPOINT"]（影响后续首次 import）
      2. 直接 patch huggingface_hub.constants.ENDPOINT（已 import 时生效）
    返回当前生效的镜像 URL（空表示走官方源）。

    同时禁用 XetHub CAS 系统（HF_HUB_DISABLE_XET=1）：
    新版 huggingface_hub（1.x）默认使用 CAS（cas-server.xethub.hf.co）下载大文件，
    但第三方镜像（如 hf-mirror.com）不代理 CAS 域名，导致 401 Unauthorized。
    禁用 Xet 后回退到传统 LFS HTTP 下载，镜像可正常代理。
    与 ENDPOINT 同理，HF_HUB_DISABLE_XET 也需直接 patch constants 常量，
    因为 huggingface_hub 在模块导入时就把环境变量读入常量固化了。
    """
    mirror = (getattr(settings, "hf_mirror_url", "") or "").strip()
    if not mirror:
        return ""
    mirror = mirror.rstrip("/")
    os.environ["HF_ENDPOINT"] = mirror
    # 禁用 XetHub CAS：镜像不代理 cas-server.xethub.hf.co，CAS 请求会 401
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        import huggingface_hub.constants as _hfc
        _hfc.ENDPOINT = mirror
        # patch 常量（环境变量在模块导入后不生效，必须直接改常量）
        _hfc.HF_HUB_DISABLE_XET = True
    except Exception as e:
        logger.debug(f"patch huggingface_hub.constants 失败: {e}")
    return mirror


def _is_valid_model_dir(path: str) -> bool:
    """校验模型目录是否完整：必须包含 config.json + model.bin"""
    required = ["config.json", "model.bin"]
    return all(os.path.isfile(os.path.join(path, f)) for f in required)


def _merge_model_chunks_if_needed(model_dir: str) -> None:
    """如果 model.bin 不存在但存在分块文件，合并它们。

    NSIS 32位编译器无法 mmap > ~1GB 的单个文件，CI 打包时会将 model.bin
    拆分成 450MB 的块（model.bin.part0, model.bin.part1, ...）。
    程序首次加载时自动合并，合并成功后删除分块以释放空间。

    注意：Tauri installMode=currentUser 安装在用户目录（可写），
    所以合并写入不会因权限问题失败。
    """
    model_bin = os.path.join(model_dir, "model.bin")
    if os.path.isfile(model_bin):
        return  # model.bin 已存在，无需合并

    import glob
    # 查找分块文件（按 part0, part1, ... 顺序排序）
    chunks = sorted(glob.glob(os.path.join(model_dir, "model.bin.part*")))
    if not chunks:
        return  # 无分块文件，走正常下载流程

    logger.info(f"发现模型分块文件，开始合并: {len(chunks)} 块 -> {model_bin}")
    try:
        with open(model_bin, "wb") as out:
            for chunk_path in chunks:
                with open(chunk_path, "rb") as f:
                    while True:
                        block = f.read(1024 * 1024)  # 1MB 缓冲区
                        if not block:
                            break
                        out.write(block)
        # 合并成功，删除分块文件释放空间
        for chunk_path in chunks:
            try:
                os.remove(chunk_path)
            except Exception as e:
                logger.debug(f"删除分块文件失败: {chunk_path}: {e}")
        logger.info(f"模型分块合并完成: {model_bin}")
    except Exception as e:
        logger.error(f"模型分块合并失败: {e}")
        # 清理不完整的 model.bin，避免后续误判
        if os.path.isfile(model_bin):
            try:
                os.remove(model_bin)
            except Exception:
                pass
        raise


def _resolve_local_model_path(model_size: str) -> Optional[str]:
    """检查本地预下载的模型目录是否存在，存在则返回绝对路径。

    查找顺序：
      1. CLIPMIND_BUNDLED_MODELS_DIR（打包内置模型目录，Tauri resources 只读）
         - 仅"带 ASR 模型版"安装包含此目录
         - 轻量版未设置此环境变量或目录为空，自动跳过
      2. CLIPMIND_DATA_DIR/models/（用户数据目录，可写）
         - 用户可手动放置模型文件实现离线运行
         - 也可从内置目录复制出来（首次使用时）

    用于离线/网络受限环境，避免每次启动都走 HuggingFace 下载。
    """
    if "/" in model_size or "\\" in model_size:
        # 已是路径形式，直接用（剥离 Windows \\?\ 前缀，避免 ctranslate2 C++ 端无法处理）
        return _normalize_windows_path(model_size) if os.path.isdir(model_size) else None

    # 1. 优先检查打包内置模型目录（带 ASR 模型版安装包）
    bundled_root = os.environ.get("CLIPMIND_BUNDLED_MODELS_DIR", "")
    if bundled_root:
        bundled_dir = os.path.join(bundled_root, f"faster-whisper-{model_size}")
        # NSIS 打包时可能将大模型文件拆分成块，首次加载时自动合并
        _merge_model_chunks_if_needed(bundled_dir)
        if _is_valid_model_dir(bundled_dir):
            return _normalize_windows_path(os.path.abspath(bundled_dir))

    # 2. 检查用户 data 目录（用户手动放置或从内置复制）
    data_dir = os.environ.get("CLIPMIND_DATA_DIR", "data")
    local_dir = os.path.join(data_dir, "models", f"faster-whisper-{model_size}")
    _merge_model_chunks_if_needed(local_dir)
    if _is_valid_model_dir(local_dir):
        return _normalize_windows_path(os.path.abspath(local_dir))
    if os.path.isdir(local_dir):
        logger.warning(f"本地模型目录不完整: {local_dir}，将走网络下载")
    return None


def _get_whisper_model(model_size: str = "small"):
    # 非法模型名（不在已知 size 列表，也不是 HuggingFace 路径形式）回退到 medium
    if "/" not in model_size and model_size not in _VALID_WHISPER_SIZES:
        logger.warning(
            f"未识别的 whisper 模型 size: {model_size}，回退到 medium"
        )
        model_size = "medium"
    if model_size in _WHISPER_MODELS:
        return _WHISPER_MODELS[model_size]
    with _WHISPER_MODEL_LOCK:
        if model_size in _WHISPER_MODELS:
            return _WHISPER_MODELS[model_size]
        from faster_whisper import WhisperModel
        device = "cpu"
        compute_type = "int8"

        # 优先用本地预下载的模型目录（离线/网络受限环境）
        local_path = _resolve_local_model_path(model_size)
        if local_path:
            logger.info(f"Loading faster-whisper from local cache: {local_path} on {device}")
            m = WhisperModel(local_path, device=device, compute_type=compute_type)
            _WHISPER_MODELS[model_size] = m
            return m

        # 本地无缓存，走 HuggingFace 下载（应用镜像）
        mirror = _apply_hf_mirror()
        if mirror:
            logger.info(f"faster-whisper 模型下载使用 HuggingFace 镜像: {mirror}")
        logger.info(f"Loading faster-whisper model: {model_size} on {device} (首次使用需下载)")
        m = WhisperModel(model_size, device=device, compute_type=compute_type)
        _WHISPER_MODELS[model_size] = m
        return m


class ASRService:
    """音频转文字服务（DashScope）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        cancel_check: CancelCheck = None,
    ):
        # ASR 使用独立的 asr_api_key，不再复用 LLM 的 openai_api_key。
        # 留空时 self.api_key 为 None，transcribe_local_file 会直接走本地 Whisper，
        # 避免 LLM 用第三方 key 时 ASR 被误调 DashScope 导致 401 浪费时间。
        self.api_key = api_key or getattr(settings, "asr_api_key", "") or None
        self.base_url = base_url or getattr(settings, "dashscope_base_url", None)
        self.model = model or getattr(settings, "asr_model", "fun-asr")
        self.timeout = timeout or getattr(settings, "asr_timeout", 300)
        # DashScope Recognition（本地文件直传）模型名：优先使用专门字段
        self.dashscope_recognition_model = getattr(
            settings, "dashscope_recognition_model", "paraformer-realtime-v2"
        )
        # 本地 faster-whisper 模型 size
        self.local_model = getattr(settings, "asr_model_local", "medium")
        self.input_format = getattr(settings, "asr_input_format", "pcm")
        self.cancel_check = cancel_check

    def _configure(self) -> None:
        if not self.api_key:
            raise ValueError("未配置 DASHSCOPE API Key")
        dashscope.api_key = self.api_key
        if self.base_url:
            dashscope.base_http_api_url = self.base_url

    def _get_output_value(self, output: Any, key: str, default=None):
        if isinstance(output, dict):
            return output.get(key, default)
        return getattr(output, key, default)

    def _transcode_audio_to_pcm(self, file_path: str) -> Optional[str]:
        """转码为 16k s16le PCM，适配 Recognition"""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.info("未检测到 ffmpeg，无法转码为 PCM")
            return None
        base, _ext = os.path.splitext(file_path)
        pcm_path = base + ".pcm"
        cmd = [
            ffmpeg,
            "-y",
            "-i", file_path,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", "16000",
            pcm_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                logger.warning(f"转码 PCM 失败: {err[:200]}")
                return None
            return pcm_path
        except Exception as e:
            logger.warning(f"转码 PCM 异常: {e}")
            return None

    def _transcode_audio_to_wav(self, file_path: str) -> Optional[str]:
        """转码为 16k 单声道 WAV"""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.info("未检测到 ffmpeg，无法转码为 WAV")
            return None
        base, _ext = os.path.splitext(file_path)
        wav_path = base + ".wav"
        cmd = [
            ffmpeg,
            "-y",
            "-i", file_path,
            "-ac", "1",
            "-ar", "16000",
            "-vn",
            wav_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                logger.warning(f"转码 WAV 失败: {err[:200]}")
                return None
            return wav_path
        except Exception as e:
            logger.warning(f"转码 WAV 异常: {e}")
            return None

    def _prepare_recognition_input(self, file_path: str) -> Optional[str]:
        """按输入格式准备 Recognition 文件"""
        fmt = (self.input_format or "pcm").lower()
        if fmt == "wav":
            return self._transcode_audio_to_wav(file_path)
        return self._transcode_audio_to_pcm(file_path)

    def _recognize_local_file(self, file_path: str) -> Optional[str]:
        """使用 Recognition 直传本地音频"""
        ensure_not_cancelled(self.cancel_check)
        self._configure()
        if not os.path.exists(file_path):
            trace_logger.warning(f"ASR 本地文件不存在: {file_path}")
            return None

        input_path = self._prepare_recognition_input(file_path)
        if not input_path:
            return None

        trace_logger.info(
            f"ASR Recognition 使用模型: {self.dashscope_recognition_model}, format={self.input_format or 'pcm'}"
        )

        try:
            from dashscope.audio.asr import Recognition
            recognizer = Recognition(
                model=self.dashscope_recognition_model,
                callback=None,
                format=(self.input_format or "pcm"),
                sample_rate=16000,
            )
            result = recognizer.call(input_path)
            ensure_not_cancelled(self.cancel_check)
            trace_logger.info(
                "ASR Recognition 结果: status_code={}, code={}, message={}, request_id={}",
                getattr(result, "status_code", None),
                getattr(result, "code", None),
                getattr(result, "message", None),
                getattr(result, "request_id", None),
            )
            sentences = result.get_sentence() or []
            if isinstance(sentences, dict):
                sentences = [sentences]
            texts = []
            for s in sentences:
                if isinstance(s, dict):
                    t = s.get("text") or ""
                    if t:
                        texts.append(t)
            text = "\n".join(texts).strip() if texts else None
            if text:
                preview = text[:120].replace("\n", " ").strip()
                trace_logger.info(f"ASR Recognition 成功，长度={len(text)}，预览：{preview}")
            return text
        except Exception as e:
            trace_logger.warning(f"ASR Recognition 异常: {e}")
            return None
        finally:
            # 只清理转码后的临时文件(input_path)，保留原始音频(file_path)
            # 原始音频由上层 _try_asr_with_local_audio 的 finally 统一清理，
            # 否则 DashScope 失败后 Whisper 兜底会因文件被删而失效
            if input_path and input_path != file_path:
                try:
                    if os.path.exists(input_path):
                        os.remove(input_path)
                except Exception:
                    trace_logger.debug(f"ASR 转码临时文件清理失败: {input_path}")

    def _download_transcription(self, url: str) -> Optional[str]:
        ensure_not_cancelled(self.cancel_check)
        # SSRF 校验：transcription_url 来自 DashScope 响应，仅允许 DashScope 域名
        # 该方法是同步函数（在 asyncio.to_thread 中执行），用同步版校验即可
        safe, reason = is_safe_dashscope_url(url)
        if not safe:
            trace_logger.warning(f"ASR 结果下载 URL SSRF 校验失败: {reason}")
            return None
        try:
            # 必须显式设置 timeout：urlopen 默认无超时，URL 无响应时会永久阻塞
            # 该方法在 asyncio.to_thread 中执行，阻塞线程池 worker
            raw = urlrequest.urlopen(url, timeout=self.timeout).read().decode("utf-8")
            data = json.loads(raw)
        except Exception as e:
            trace_logger.warning(f"ASR 结果下载失败: {e}")
            return None

        texts = []
        transcripts = data.get("transcripts") or []
        for item in transcripts:
            text = item.get("text", "") or ""
            if text:
                texts.append(text)
                continue
            for s in item.get("sentences", []) or []:
                s_text = s.get("text", "") or ""
                if s_text:
                    texts.append(s_text)

        if not texts and isinstance(data.get("text"), str):
            texts.append(data["text"])

        return "\n".join(texts).strip() if texts else None

    def _build_api_url(self, *parts: str) -> str:
        base_url = self.base_url or getattr(dashscope, "base_http_api_url", None)
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com/api/v1"
        return _join_url(base_url, *parts)

    def _submit_transcription_task_restful(self, audio_url: str, model: str) -> Optional[str]:
        url = self._build_api_url("services", "audio", "asr", "transcription")
        headers = {
            **_default_headers(self.api_key),
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        parameters = {}
        if "paraformer" in model:
            parameters["language_hints"] = ["zh", "en"]
        payload = {"model": model, "input": {"file_urls": [audio_url]}}
        if parameters:
            payload["parameters"] = parameters

        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=30.0, trust_env=True)
        except Exception as e:
            trace_logger.warning(f"ASR RESTful 提交失败: {e}")
            return None

        if resp.status_code != HTTPStatus.OK:
            trace_logger.warning(f"ASR RESTful 提交失败: status_code={resp.status_code}, body={resp.text[:300]}")
            return None

        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            output = data.get("output") if isinstance(data, dict) else None
            if isinstance(output, dict):
                task_id = output.get("task_id")
        return task_id

    def _fetch_transcription_task_restful(self, task_id: str) -> Optional[dict]:
        url = self._build_api_url("tasks", task_id)
        headers = _default_headers(self.api_key)
        try:
            resp = httpx.get(url, headers=headers, timeout=30.0, trust_env=True)
        except Exception as e:
            trace_logger.warning(f"ASR RESTful 查询失败: {e}")
            return None

        if resp.status_code != HTTPStatus.OK:
            trace_logger.warning(f"ASR RESTful 查询失败: status_code={resp.status_code}, body={resp.text[:300]}")
            return None

        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("output"), dict):
            return data["output"]
        return data if isinstance(data, dict) else None

    async def _transcribe_local_whisper(self, audio_path: str) -> Optional[str]:
        """Use faster-whisper locally for ASR (no API key needed).

        模型名称取自用户配置的 asr_model_local（如 tiny/base/small/medium/large-v3），
        未配置时回退到 "medium"。支持 HuggingFace 模型 ID（含路径斜杠）。

        语言检测策略：
        - 默认让 faster-whisper 自动检测语言（前 30 秒音频做语种识别）
        - 不再硬编码 language="zh"，否则英文/混合语种视频会被错误地按中文模式解码
        - 自动检测在中文视频上准确率很高，且对跨语言内容更友好

        性能优化：
        - .m4s 等 MPEG-DASH 格式直接喂给 faster-whisper 会触发内部 ffmpeg 解码，
          但格式不友好导致解码慢。先用 ffmpeg 预转码为 16kHz 单声道 WAV，
          faster-whisper 原生支持 WAV，可显著提升转写速度。
        - beam_size=1（贪心解码）比 beam_size=3 快 2-3 倍，质量损失可接受。
        - 启用 VAD 滤波跳过静音段，减少无效计算。

        超时实现：
        - 使用 asyncio.wait_for + asyncio.to_thread 实现真正的超时。
        - 旧实现用 concurrent.futures.ThreadPoolExecutor 的 context manager，
          其 __exit__ 调用 shutdown(wait=True) 会阻塞直到 Whisper 线程结束，
          导致超时形同虚设——即使 future.result 超时，函数仍会阻塞到 Whisper 完成。
        - 新实现超时后协程立即返回 None，Whisper 线程在后台继续运行
          （Python 无法强制终止线程），但不阻塞入库主流程。
        """
        ensure_not_cancelled(self.cancel_check)
        if not os.path.exists(audio_path):
            trace_logger.warning(f"Whisper: file not found: {audio_path}")
            return None
        model_name = self.local_model or "medium"

        # 预转码：将 .m4s 等非 WAV 格式转为 16kHz 单声道 WAV，提升 Whisper 解码速度
        wav_path = self._pretranscode_to_wav(audio_path)
        transcribe_path = wav_path or audio_path

        # local Whisper 使用独立超时（asr_whisper_timeout，默认 300s）
        whisper_timeout = max(120, int(getattr(settings, "asr_whisper_timeout", 300) or 300))
        try:
            model = _get_whisper_model(model_name)

            def _run():
                # beam_size=1 贪心解码，比默认 3 快 2-3x
                # vad_filter=True 跳过静音段，减少计算量
                segs, info = model.transcribe(
                    transcribe_path,
                    beam_size=1,
                    vad_filter=True,
                )
                collected = [seg.text.strip() for seg in segs]
                return collected, info

            # asyncio.wait_for 超时后立即返回，不阻塞事件循环
            try:
                texts, info = await asyncio.wait_for(
                    asyncio.to_thread(_run),
                    timeout=whisper_timeout,
                )
            except asyncio.TimeoutError:
                trace_logger.warning(
                    f"[ASR] local Whisper 转码超时（{whisper_timeout}秒），跳过此视频: {audio_path}"
                )
                return None

            result = " ".join(texts).strip() if texts else ""
            if result:
                trace_logger.info(
                    f"Whisper ASR completed: {len(result)} chars, detected language: {getattr(info, 'language', 'unknown')}"
                )
                return result
        except Exception as e:
            trace_logger.warning(f"Whisper ASR failed: {e}")
        finally:
            # 清理预转码产生的临时 WAV 文件
            if wav_path and wav_path != audio_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
        return None

    def _pretranscode_to_wav(self, audio_path: str) -> Optional[str]:
        """将非 WAV 音频预转码为 16kHz 单声道 WAV，提升 Whisper 解码速度。

        faster-whisper 内部通过 ffmpeg 解码音频，但对 .m4s（MPEG-DASH 片段）
        等格式解码效率低。预转码为标准 WAV 后，faster-whisper 可直接读取 PCM 数据，
        省去每次 transcribe 时的格式探测和解码开销。

        若 ffmpeg 不可用或转码失败，返回 None，回退到直接传原文件给 Whisper。
        """
        # 已是 WAV 则不需要转码
        ext = os.path.splitext(audio_path)[1].lower()
        if ext == ".wav":
            return None

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None

        wav_path = os.path.splitext(audio_path)[0] + "_whisper.wav"
        cmd = [
            ffmpeg, "-y", "-i", audio_path,
            "-ac", "1", "-ar", "16000",
            "-vn",  # 丢弃视频流（如果有）
            wav_path,
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=60,
            )
            if result.returncode == 0 and os.path.exists(wav_path):
                trace_logger.info(f"[ASR] 预转码 WAV 成功: {os.path.getsize(wav_path)} bytes")
                return wav_path
            trace_logger.debug(f"[ASR] 预转码 WAV 失败 (rc={result.returncode}): {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            trace_logger.warning("[ASR] 预转码 WAV 超时（60s），回退到直接传原文件")
        except Exception as e:
            trace_logger.debug(f"[ASR] 预转码 WAV 异常: {e}")
        # 清理失败的临时文件
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception:
                pass
        return None

    def _transcribe_sync_restful(self, audio_url: str, model: str) -> Optional[str]:
        ensure_not_cancelled(self.cancel_check)
        self._configure()
        task_id = self._submit_transcription_task_restful(audio_url, model)
        if not task_id:
            trace_logger.warning("ASR RESTful 未返回 task_id")
            return None
        trace_logger.info(f"ASR 任务已提交(RESTful): task_id={task_id}")

        start = time.time()
        output = None
        while True:
            ensure_not_cancelled(self.cancel_check)
            if time.time() - start > self.timeout:
                trace_logger.warning("ASR 任务超时(RESTful)")
                return None
            output = self._fetch_transcription_task_restful(task_id)
            ensure_not_cancelled(self.cancel_check)
            if not output:
                time.sleep(1.5)
                continue
            status = self._get_output_value(output, "task_status")
            if status in ("SUCCEEDED", "FAILED"):
                break
            time.sleep(1.5)

        results = self._get_output_value(output, "results", []) or []
        status_message = self._get_output_value(output, "status_message")
        trace_logger.info(
            "ASR 任务状态(RESTful): task_id={}, task_status={}, status_code={}, status_message={}, results={}",
            task_id,
            self._get_output_value(output, "task_status"),
            HTTPStatus.OK,
            status_message,
            len(results),
        )
        for item in results:
            sub_status = item.get("subtask_status")
            transcription_url = item.get("transcription_url")
            error_message = item.get("error_message") or item.get("message")
            if sub_status:
                trace_logger.info(
                    "ASR 子任务状态(RESTful): task_id={}, subtask_status={}, has_url={}, error={}",
                    task_id,
                    sub_status,
                    bool(transcription_url),
                    error_message,
                )
            if sub_status == "SUCCEEDED" and transcription_url:
                return self._download_transcription(transcription_url)

        trace_logger.warning("ASR 未返回有效转写结果(RESTful)")
        return None

    def _transcribe_sync(self, audio_url: str) -> Optional[str]:
        ensure_not_cancelled(self.cancel_check)
        self._configure()
        if audio_url.startswith("oss://"):
            return self._transcribe_sync_restful(audio_url, self.model)

        kwargs = {}
        if "paraformer" in self.model:
            kwargs["language_hints"] = ["zh", "en"]

        try:
            from dashscope.audio.asr import Transcription
            resp = Transcription.async_call(
                model=self.model,
                file_urls=[audio_url],
                **kwargs,
            )
        except Exception as e:
            trace_logger.warning(f"ASR 提交失败: {e}")
            return None

        output = getattr(resp, "output", None)
        task_id = self._get_output_value(output, "task_id")
        if not task_id:
            trace_logger.warning("ASR 未返回 task_id")
            return None
        trace_logger.info(f"ASR 任务已提交: task_id={task_id}")

        start = time.time()
        while True:
            ensure_not_cancelled(self.cancel_check)
            status = self._get_output_value(output, "task_status")
            if status in ("SUCCEEDED", "FAILED"):
                break
            if time.time() - start > self.timeout:
                trace_logger.warning("ASR 任务超时")
                return None
            time.sleep(1.5)
            from dashscope.audio.asr import Transcription
            resp = Transcription.fetch(task=task_id)
            ensure_not_cancelled(self.cancel_check)
            output = getattr(resp, "output", None)

        status_code = getattr(resp, "status_code", None)
        if status_code != HTTPStatus.OK:
            trace_logger.warning(f"ASR 请求失败: status_code={status_code}")
            return None

        results = self._get_output_value(output, "results", []) or []
        status_message = self._get_output_value(output, "status_message")
        trace_logger.info(
            "ASR 任务状态: task_id={}, task_status={}, status_code={}, status_message={}, results={}",
            task_id,
            self._get_output_value(output, "task_status"),
            status_code,
            status_message,
            len(results),
        )
        for item in results:
            sub_status = item.get("subtask_status")
            transcription_url = item.get("transcription_url")
            error_message = item.get("error_message") or item.get("message")
            if sub_status:
                trace_logger.info(
                    "ASR 子任务状态: task_id={}, subtask_status={}, has_url={}, error={}",
                    task_id,
                    sub_status,
                    bool(transcription_url),
                    error_message,
                )
            if sub_status == "SUCCEEDED" and transcription_url:
                return self._download_transcription(item["transcription_url"])

        trace_logger.warning("ASR 未返回有效转写结果")
        return None

    async def transcribe_url(self, audio_url: str) -> Optional[str]:
        result = await asyncio.to_thread(self._transcribe_sync, audio_url)
        ensure_not_cancelled(self.cancel_check)
        return result

    async def transcribe_local_file(self, file_path: str) -> Optional[str]:
        """本地文件识别：优先 DashScope Recognition，失败则回退到本地 Whisper"""
        # 原 `self.api_key != settings.openai_api_key` 判断永远为 False（构造函数默认 api_key = openai_api_key），
        # 导致即使用户配置了 DashScope key 也永远走 Whisper 本地分支。
        # 改为：只要 api_key 非空就尝试 DashScope，由 _recognize_local_file 内部失败时回退。
        if self.api_key:
            result = await asyncio.to_thread(self._recognize_local_file, file_path)
            ensure_not_cancelled(self.cancel_check)
            if result:
                return result
            trace_logger.info("DashScope ASR failed, falling back to local Whisper...")
        else:
            trace_logger.info("No valid DashScope key, using local Whisper for ASR...")

        # Fallback to local Whisper（_transcribe_local_whisper 已改为 async，
        # 内部用 asyncio.wait_for + asyncio.to_thread 实现真正的超时）
        result = await self._transcribe_local_whisper(file_path)
        ensure_not_cancelled(self.cancel_check)
        return result
