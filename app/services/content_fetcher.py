"""
Bilibili RAG 知识库系统

视频内容获取服务 - 二级降级策略
"""
from typing import Optional, Callable
from urllib.parse import urlparse
import os
import time
import httpx
from loguru import logger
from app.models import VideoContent, ContentSource
from app.services.bilibili import BilibiliService
from app.services.asr import ASRService
from app.services.cancellation import CancelCheck, ensure_not_cancelled
from app.services.tracing import trace_logger
from app.services.url_safety import is_safe_bilibili_url_async


class ContentFetcher:
    """
    视频内容获取器
    
    采用二级降级策略：
    1. 音频转写（ASR）
    2. 视频基本信息 (兜底)
    """
    
    def __init__(
        self,
        bilibili_service: BilibiliService,
        asr_service: ASRService,
        cancel_check: CancelCheck = None,
    ):
        self.bili = bilibili_service
        self.asr = asr_service
        self.cancel_check = cancel_check
    
    async def fetch_content(
        self,
        bvid: str,
        cid: int = None,
        title: str = None,
        description: str = None,
        owner_name: str = None,
        owner_mid: int = None,
        duration: int = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> VideoContent:
        """
        获取视频内容，自动降级

        Args:
            bvid: 视频 BV 号
            cid: 视频 cid (如果没有会自动获取)
            title: 视频标题 (如果没有会自动获取)
            progress_callback: 可选进度回调，在 ASR 等耗时阶段触发，参数为状态描述文本

        Returns:
            VideoContent 对象
        """
        ensure_not_cancelled(self.cancel_check)
        # 获取视频基本信息。即使调用方传了 cid，也需要读取 pages 来识别多 P 视频。
        video_info = None
        try:
            video_info = await self.bili.get_video_info(bvid)
            ensure_not_cancelled(self.cancel_check)
            if not cid:
                cid = video_info.get("cid")
            if not title:
                title = video_info.get("title", "未知标题")
        except Exception as e:
            if not cid or not title:
                trace_logger.error(f"获取视频信息失败 [{bvid}]: {e}")
                return VideoContent(
                    bvid=bvid,
                    title=title or "未知标题",
                    content="无法获取视频信息",
                    source=ContentSource.BASIC_INFO,
                    platform="bilibili",
                    description=description,
                    owner_name=owner_name,
                    owner_mid=owner_mid,
                    duration=duration,
                )
            trace_logger.warning(f"获取视频信息失败 [{bvid}]，将按单 P 处理: {e}")
        
        owner = (video_info.get("owner") or {}) if video_info else {}
        description = description or (video_info.get("desc", "") if video_info else "")
        owner_name = owner_name or owner.get("name")
        owner_mid = owner_mid or owner.get("mid")
        duration = duration or (video_info.get("duration") if video_info else None)
        
        # Level 1: 跳过 AI 摘要，优先使用 ASR
        trace_logger.info(f"[{bvid}] 已跳过 AI 摘要，优先使用 ASR")

        pages = self._extract_video_pages(video_info, cid)
        if len(pages) > 1:
            asr_text = await self._try_multi_part_asr(bvid, pages, title=title, progress_callback=progress_callback)
        else:
            asr_cid = pages[0]["cid"] if pages else cid
            asr_text = await self._try_asr(bvid, asr_cid, title=title, progress_callback=progress_callback)
        ensure_not_cancelled(self.cancel_check)
        if asr_text:
            trace_logger.info(f"[{bvid}] 使用 ASR 文本")
            return VideoContent(
                bvid=bvid,
                title=title,
                content=asr_text,
                source=ContentSource.ASR,
                platform="bilibili",
                description=description,
                owner_name=owner_name,
                owner_mid=owner_mid,
                duration=duration,
            )
        
        # ASR 失败时，补齐基础信息（避免遗漏简介）
        if not video_info:
            try:
                video_info = await self.bili.get_video_info(bvid)
                ensure_not_cancelled(self.cancel_check)
            except Exception as e:
                trace_logger.debug(f"[{bvid}] 获取视频信息失败(兜底): {e}")

        if video_info and not description:
            description = video_info.get("desc", "") or description

        # Level 3: 使用基本信息兜底
        trace_logger.info(f"[{bvid}] 使用基本信息")
        basic_content = f"视频标题：{title}"
        if description:
            basic_content += f"\n\n视频简介：{description}"
        
        return VideoContent(
            bvid=bvid,
            title=title,
            content=basic_content,
            source=ContentSource.BASIC_INFO,
            platform="bilibili",
            description=description,
            owner_name=owner_name,
            owner_mid=owner_mid,
            duration=duration,
        )

    def _extract_video_pages(self, video_info: Optional[dict], fallback_cid: Optional[int]) -> list[dict]:
        """从视频详情中提取分 P 信息。"""
        pages = []
        for index, page in enumerate((video_info or {}).get("pages") or [], start=1):
            page_cid = page.get("cid")
            if not page_cid:
                continue
            pages.append({
                "cid": page_cid,
                "page": page.get("page") or index,
                "part": page.get("part") or f"P{index}",
            })
        if pages:
            return pages
        return [{"cid": fallback_cid, "page": 1, "part": "P1"}] if fallback_cid else []

    async def _try_multi_part_asr(
        self,
        bvid: str,
        pages: list[dict],
        title: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """逐个分 P 转写并合并文本。"""
        trace_logger.info(f"[{bvid}] 检测到多 P 视频，共 {len(pages)} P，开始逐 P ASR")
        parts = []
        for page in pages:
            ensure_not_cancelled(self.cancel_check)
            page_no = page["page"]
            part_title = page["part"]
            text = await self._try_asr(bvid, page["cid"], title=title or part_title, progress_callback=progress_callback)
            ensure_not_cancelled(self.cancel_check)
            if text:
                parts.append(f"## P{page_no} {part_title}\n\n{text}")
            else:
                trace_logger.warning(f"[{bvid}] P{page_no} ASR 未获取到有效文本")
        return "\n\n".join(parts).strip() or None

    async def _try_asr(
        self,
        bvid: str,
        cid: int,
        title: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """尝试进行音频转写"""
        try:
            ensure_not_cancelled(self.cancel_check)
            # ASR 转码耗时较长，提前触发回调让入库任务感知当前阶段
            if progress_callback:
                progress_callback(f"ASR 转码中: {title or bvid}")
            audio_url = await self.bili.get_audio_url(bvid, cid)
            ensure_not_cancelled(self.cancel_check)
            if not audio_url:
                trace_logger.info(f"[{bvid}] 未获取到音频 URL")
                return None
            status = await self._probe_audio_url(bvid, audio_url)
            ensure_not_cancelled(self.cancel_check)
            if status is not None and status < 400:
                trace_logger.info(f"[{bvid}] 音频 URL 可达，使用 Transcription")
                text = await self.asr.transcribe_url(audio_url)
            else:
                trace_logger.info(f"[{bvid}] 音频 URL 不可达，使用 Recognition 兜底")
                text = await self._try_asr_with_local_audio(bvid, cid, audio_url)
            ensure_not_cancelled(self.cancel_check)

            # ASR 最小有效长度阈值：
            # - 原 50 字符对短视频（<60s）过于激进，正常短视频可能只有 20-30 字符
            # - 降到 20 字符，能保留短视频的有效内容，同时过滤纯噪声/空白结果
            # - rag.add_video_content 内部还有 <10 的兜底检查，双重保险
            if not text or len(text.strip()) < 20:
                trace_logger.info(f"[{bvid}] ASR 内容过少(len={len(text) if text else 0})")
                return None
            preview = text[:120].replace("\n", " ").strip()
            trace_logger.info(f"[{bvid}] ASR 成功，长度={len(text)}，预览：{preview}")
            return text
        except Exception as e:
            trace_logger.warning(f"[{bvid}] ASR 失败: {e}")
            return None

    async def _probe_audio_url(self, bvid: str, audio_url: str) -> Optional[int]:
        """探测音频 URL 可达性（不带 Cookie，模拟 ASR 服务拉取）"""
        # SSRF 校验：仅允许 B 站 CDN 域名，拒绝内网 IP
        # 用 async 版本，DNS 解析放到线程池避免阻塞事件循环
        safe, reason = await is_safe_bilibili_url_async(audio_url)
        if not safe:
            trace_logger.warning(f"[{bvid}] 音频 URL SSRF 校验失败: {reason}")
            return None

        try:
            parsed = urlparse(audio_url)
            safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except Exception:
            safe_url = "unknown"

        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            status = None
            try:
                head = await client.head(audio_url)
                status = head.status_code
            except Exception as e:
                trace_logger.info(f"[{bvid}] 音频 URL HEAD 失败: {e}")

            if status is None or status >= 400:
                try:
                    headers = {"Range": "bytes=0-0"}
                    get = await client.get(audio_url, headers=headers)
                    status = get.status_code
                except Exception as e:
                    trace_logger.info(f"[{bvid}] 音频 URL GET 失败: {e}")

        if status is None:
            trace_logger.info(f"[{bvid}] 音频 URL 不可达: {safe_url}")
        else:
            trace_logger.info(f"[{bvid}] 音频 URL 可达性: {status} - {safe_url}")
        return status

    async def _try_asr_with_local_audio(
        self, bvid: str, cid: int, audio_url: str
    ) -> Optional[str]:
        """本地下载后使用 Recognition 直传"""
        ensure_not_cancelled(self.cancel_check)
        # SSRF 校验：_probe_audio_url 已校验过同一 URL，但这里防御性再校验一次
        # （未来该方法可能被单独调用）。DNS 结果有 TTL 缓存，重复调用开销极小。
        safe, reason = await is_safe_bilibili_url_async(audio_url)
        if not safe:
            trace_logger.warning(f"[{bvid}] 本地下载音频 URL SSRF 校验失败: {reason}")
            return None

        tmp_dir = os.path.join("data", "asr_tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            parsed = urlparse(audio_url)
            ext = os.path.splitext(parsed.path)[1] or ".m4s"
        except Exception:
            ext = ".m4s"

        filename = f"{bvid}_{cid}_{int(time.time())}{ext}"
        file_path = os.path.join(tmp_dir, filename)

        try:
            ok = await self.bili.download_audio_to_file(audio_url, file_path)
            ensure_not_cancelled(self.cancel_check)
            if not ok:
                trace_logger.info(f"[{bvid}] 本地下载音频失败")
                return None

            if os.path.exists(file_path) and os.path.getsize(file_path) < 1024:
                trace_logger.info(f"[{bvid}] 本地音频文件过小，跳过上传")
                return None

            text = await self.asr.transcribe_local_file(file_path)
            ensure_not_cancelled(self.cancel_check)
            if text:
                preview = text[:120].replace("\n", " ").strip()
                trace_logger.info(f"[{bvid}] Recognition ASR 成功，长度={len(text)}，预览：{preview}")
            return text
        finally:
            # 所有路径统一清理临时文件，避免磁盘空间泄露
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                trace_logger.debug(f"[{bvid}] 清理临时音频失败: {file_path}")

    # 已删除以下死代码（无外部调用）：
    # - _transcode_audio_to_wav / _get_audio_duration_sec / _split_audio_wav
    #   （音频分段转码逻辑，已被 _try_asr_with_local_audio 直接调用 ffmpeg 替代）
    # - _try_ai_summary / _try_subtitle （fetch_content 第 93 行已注释跳过 AI 摘要/字幕，
    #   优先使用 ASR）
    # - fetch_all_videos_content （被 knowledge.py 的 _sync_folder 逐个调用替代）

