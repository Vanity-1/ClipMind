"""
Bilibili RAG 知识库系统

RAG 服务模块 - 向量存储与问答
"""
import os
from typing import Callable, List, Optional
from loguru import logger
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.documents import Document
from app.config import settings
from app.models import VideoContent, utcnow
from app.services.cancellation import OperationCancelled, ensure_not_cancelled
from app.services.tracing import trace_logger


def _build_video_url(platform: str, bvid: str) -> str:
    """根据平台生成视频 URL"""
    if platform == "douyin":
        return f"https://www.douyin.com/video/{bvid}"
    return f"https://www.bilibili.com/video/{bvid}"


class RAGService:
    """
    RAG 服务
    
    负责：
    1. 向量存储管理
    2. 文档添加与检索
    3. 问答功能
    """
    
    _VALID_PROVIDERS = {"openai", "dashscope", "ollama", "nvidia", "local"}

    def __init__(self, collection_name: str = "bilibili_videos"):
        """
        初始化 RAG 服务
        
        Args:
            collection_name: 向量集合名称
        """
        # 热加载兼容：模块级 settings 是 import 时的快照，reload_settings() 会替换
        # app.config.settings 但不会更新本模块的 settings 变量。
        # 始终从 app.config 取最新实例；测试若 patch 了 app.services.rag.settings
        # 则优先用 patch 的值（向下兼容）。
        _s = globals().get("settings")
        import app.config as _cfg
        if _s is None or _s is not _cfg.settings:
            _s = _cfg.settings

        self.collection_name = collection_name
        
        # 初始化 Embeddings -- 优先按 settings.embedding_provider 选择后端
        # 回退兼容：provider 为空或 None 时按模型前缀路由（保持旧逻辑）
        raw_provider = getattr(_s, "embedding_provider", "") or ""
        if isinstance(raw_provider, str):
            emb_provider = raw_provider.strip().lower()
        else:
            emb_provider = ""
        emb_model = ((_s.embedding_model if isinstance(_s.embedding_model, str) else "") or "").strip()
        _emb_key = _s.embedding_api_key if isinstance(_s.embedding_api_key, str) else ""
        _open_key = _s.openai_api_key if isinstance(_s.openai_api_key, str) else ""
        emb_key = _emb_key or _open_key
        _emb_url = _s.embedding_base_url if isinstance(_s.embedding_base_url, str) else ""
        _open_url = _s.openai_base_url if isinstance(_s.openai_base_url, str) else ""
        emb_url = _emb_url or _open_url

        # 显式 provider 模式：无效 provider 抛出 ValueError
        if emb_provider and emb_provider not in RAGService._VALID_PROVIDERS:
            raise ValueError(
                f"无效的 embedding_provider: '{emb_provider}'，"
                f"有效值: {sorted(RAGService._VALID_PROVIDERS)}"
            )

        if not emb_provider:
            # 空字符串回退：按默认 openai provider（不崩溃）
            emb_provider = "openai"

        if emb_provider == "nvidia" or emb_model.startswith("nvidia/"):
            from app.services.nvidia_embeddings import NVIDIAEmbeddings
            emb_url = emb_url or "https://integrate.api.nvidia.com/v1"
            model_for_nv = emb_model if emb_model.startswith("nvidia/") else f"nvidia/{emb_model or 'nvidia/embed-qa-4'}"
            self.embeddings = NVIDIAEmbeddings(
                model=model_for_nv,
                api_key=emb_key,
                base_url=emb_url,
            )
            logger.info(f"使用 NVIDIAEmbeddings 初始化成功 (model={model_for_nv})")
        elif emb_provider == "dashscope" or (emb_model.startswith("text-embedding-v") and "dashscope" in (emb_url or "").lower()):
            # DashScope 私有协议（text-embedding-v1/v2/v3/v4 + DashScope endpoint）
            try:
                from langchain_community.embeddings import DashScopeEmbeddings
            except ImportError as exc:
                logger.error("缺少 langchain-community，无法初始化 DashScope Embedding")
                raise RuntimeError(
                    "DashScope Embedding 初始化失败，请运行 pip install -r requirements.txt"
                ) from exc
            ds_key = getattr(_s, "dashscope_api_key", "") or emb_key
            ds_model = emb_model or "text-embedding-v2"
            self.embeddings = DashScopeEmbeddings(
                dashscope_api_key=ds_key,
                model=ds_model,
            )
            logger.info(f"使用 DashScopeEmbeddings 初始化成功 (model={ds_model})")
        elif emb_provider == "ollama":
            # Ollama 本地模型（默认 http://localhost:11434）
            try:
                from langchain_community.embeddings import OllamaEmbeddings
            except ImportError as exc:
                logger.error("缺少 langchain-community，无法初始化 Ollama Embedding")
                raise RuntimeError(
                    "Ollama Embedding 初始化失败，请运行 pip install -r requirements.txt"
                ) from exc
            ollama_url = emb_url or "http://localhost:11434"
            ollama_model = emb_model or "nomic-embed-text"
            self.embeddings = OllamaEmbeddings(
                base_url=ollama_url,
                model=ollama_model,
            )
            logger.info(f"使用 OllamaEmbeddings 初始化成功 (model={ollama_model}, base_url={ollama_url})")
        elif emb_provider == "local":
            # 本地向量模型（bge-small-zh / m3e-base 等）
            # 由模型市场下载到 data/models/embeddings/<id>/，embedding_model 字段存本地路径
            if not emb_model or not os.path.isdir(emb_model):
                raise RuntimeError(
                    f"本地向量模型路径无效: {emb_model!r}。"
                    f"请先在模型市场下载向量模型，或检查 embedding_model 配置。"
                )
            # 预检权重文件：目录存在但缺权重文件时给出明确提示，
            # 避免底层抛出英文异常让人困惑
            _pt_weight_files = ("model.safetensors", "pytorch_model.bin", "model.bin")
            _onnx_weight_files = (
                os.path.join("onnx", "model.onnx"),
                os.path.join("onnx", "model_quantized.onnx"),
                "model.onnx",
            )
            _has_pt = any(
                os.path.exists(os.path.join(emb_model, wf)) for wf in _pt_weight_files
            )
            _has_onnx = any(
                os.path.exists(os.path.join(emb_model, wf)) for wf in _onnx_weight_files
            )
            if not _has_pt and not _has_onnx:
                raise RuntimeError(
                    f"本地向量模型不完整：目录 {emb_model} 中缺少权重文件"
                    f"（model.safetensors / model.onnx）。请在模型市场重新下载该模型。"
                )

            # 优先 ONNX Runtime 推理（安装包内置 onnxruntime，不依赖 torch）。
            # 仅当模型无 ONNX 权重时才回退到 sentence-transformers。
            if _has_onnx:
                try:
                    from app.services.local_onnx_embeddings import LocalOnnxEmbeddings
                    self.embeddings = LocalOnnxEmbeddings(
                        model_dir=emb_model,
                        model_name=os.path.basename(emb_model.rstrip("/\\")),
                    )
                    logger.info(
                        f"使用 LocalOnnxEmbeddings 初始化成功 "
                        f"(model={emb_model}, onnx={self.embeddings.onnx_path})"
                    )
                except Exception as exc:
                    # ONNX 初始化失败（如缺 onnxruntime），回退到 HuggingFaceEmbeddings
                    logger.warning(
                        f"ONNX Embedding 初始化失败，回退到 HuggingFaceEmbeddings: {exc}"
                    )
                    try:
                        from langchain_huggingface import HuggingFaceEmbeddings
                    except ImportError as exc2:
                        raise RuntimeError(
                            "本地 Embedding 初始化失败：ONNX 推理不可用"
                            f"（{exc}），且缺少 sentence-transformers（{exc2}）。"
                            "请安装 onnxruntime 后重试，或切换到 API Embedding 模式。"
                        ) from exc2
                    self.embeddings = HuggingFaceEmbeddings(
                        model_name=emb_model,
                        encode_kwargs={"normalize_embeddings": True},
                    )
                    logger.info(
                        f"使用 HuggingFaceEmbeddings 初始化成功 (model={emb_model})"
                    )
            else:
                # 无 ONNX 权重（如 m3e-base），必须走 sentence-transformers
                try:
                    from langchain_huggingface import HuggingFaceEmbeddings
                except ImportError as exc:
                    logger.error("缺少 langchain-huggingface，无法初始化本地 Embedding")
                    raise RuntimeError(
                        "本地 Embedding 初始化失败：该模型无 ONNX 权重，"
                        "需要 sentence-transformers。请安装依赖后重试，"
                        "或切换到 API Embedding 模式。"
                    ) from exc
                self.embeddings = HuggingFaceEmbeddings(
                    model_name=emb_model,
                    encode_kwargs={"normalize_embeddings": True},
                )
                logger.info(
                    f"使用 HuggingFaceEmbeddings 初始化成功 (model={emb_model})"
                )
        else:
            # OpenAI 兼容协议（OpenAI 官方、Azure、第三方兼容 API）
            # 覆盖 text-embedding-3-small 等模型，与测试端点协议一致
            from app.services.openai_embeddings import OpenAICompatibleEmbeddings
            self.embeddings = OpenAICompatibleEmbeddings(
                model=emb_model or "text-embedding-3-small",
                api_key=emb_key,
                base_url=emb_url or "https://api.openai.com/v1",
            )
            logger.info(f"使用 OpenAICompatibleEmbeddings 初始化成功 (model={emb_model}, base_url={emb_url})")
        
        # 初始化向量存储
        self.vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=_s.chroma_persist_directory
        )
        
        # 初始化 LLM
        _llm_key = _s.openai_api_key if isinstance(_s.openai_api_key, str) else ""
        _llm_base_url = _s.openai_base_url if isinstance(_s.openai_base_url, str) else ""
        _llm_model = _s.llm_model if isinstance(_s.llm_model, str) else ""

        # 诊断日志：打印 key 长度（不打印 key 本身）+ base_url + model
        logger.info(
            f"[RAG] LLM 配置: key_len={len(_llm_key)}, "
            f"base_url={_llm_base_url}, model={_llm_model}"
        )

        # 读取 llm_provider：参考 embedding_provider 的取值方式
        _llm_provider = (getattr(_s, "llm_provider", "") or "").strip().lower() if isinstance(
            getattr(_s, "llm_provider", ""), str) else ""

        if _llm_provider == "ollama":
            # Ollama 本地模式：不需要 API Key，使用 langchain_ollama.ChatOllama
            try:
                from langchain_ollama import ChatOllama
            except ImportError as exc:
                logger.error("[RAG] 缺少 langchain-ollama，无法初始化 Ollama LLM")
                raise RuntimeError(
                    "Ollama LLM 初始化失败，需安装 langchain-ollama: pip install langchain-ollama"
                ) from exc
            _ollama_url = (getattr(_s, "ollama_base_url", "") or "").strip() or "http://localhost:11434"
            _ollama_model = (getattr(_s, "ollama_model", "") or "").strip() or "qwen2.5:7b"
            logger.info(
                f"[RAG] 使用 ChatOllama 初始化: model={_ollama_model}, base_url={_ollama_url}"
            )
            try:
                self.llm = ChatOllama(
                    base_url=_ollama_url,
                    model=_ollama_model,
                    temperature=0.5,
                )
            except ValueError:
                # 配置缺失的 ValueError 直接向上抛，不吞掉
                raise
            except Exception as e:
                logger.error(f"[RAG] ChatOllama 创建失败: {e}")
                raise
        else:
            # API 模式：保持原有 ChatOpenAI 行为
            if not _llm_key:
                raise ValueError("未配置 LLM API Key（openai_api_key），请在设置中配置后再入库")

            try:
                self.llm = ChatOpenAI(
                    api_key=_s.openai_api_key,
                    base_url=_s.openai_base_url,
                    model=_s.llm_model,
                    temperature=0.5
                )
            except ValueError:
                # 配置缺失的 ValueError 直接向上抛，不吞掉
                raise
            except Exception as e:
                logger.error(f"[RAG] ChatOpenAI 创建失败: {e}")
                raise
        
        # 文本分割器：使用 settings 中的 chunk_size/chunk_overlap，缺省回退 280/50
        cs = getattr(_s, "chunk_size", 280) or 280
        co = getattr(_s, "chunk_overlap", 50) or 50
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=int(cs),
            chunk_overlap=int(co),
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " "]
        )
        
        # 问答/摘要提示模板与对应的 answer_question/summarize_content/_fallback_answer
        # 方法已迁移到 app/routers/chat.py（采用流式 OpenAI 调用），原方法为死代码已删除。

    def _build_metadata_document(self, video: VideoContent) -> Optional[Document]:
        """Build a compact searchable metadata document for title/intro recall."""
        parts = [f"视频标题：{video.title or '未知标题'}"]
        if video.owner_name:
            parts.append(f"UP主：{video.owner_name}")
        if video.description:
            parts.append(f"视频简介：{video.description}")
        if video.duration:
            parts.append(f"视频时长：{video.duration} 秒")
        if video.outline:
            outline_titles = []
            for item in video.outline:
                title = (item.get("title") or "").strip() if isinstance(item, dict) else ""
                if title:
                    outline_titles.append(title)
            if outline_titles:
                parts.append("内容提纲：" + "；".join(outline_titles[:8]))

        content = "\n".join(part for part in parts if part).strip()
        if len(content) < 10:
            return None

        return Document(
            page_content=content,
            metadata={
                "bvid": video.bvid,
                "title": video.title or "未知标题",
                "source": video.source.value,
                "platform": video.platform,
                "doc_type": "metadata",
                "chunk_index": -1,
                "url": _build_video_url(video.platform, video.bvid),
            },
        )
    
    def prepare_documents(self, video: VideoContent) -> List[Document]:
        """根据 VideoContent 生成待写入的文档列表（含元信息文档 + 内容分块）。

        纯计算逻辑，不触碰向量库。提取出来是为了让调用方在 delete 旧向量之前
        做预校验：如果 prepare_documents 返回空列表，说明内容不足以生成任何文档，
        此时不应该 delete 旧向量，否则会造成"先删后加失败"的数据丢失窗口。

        Returns:
            待写入的 Document 列表。空列表表示内容无效，应跳过写入。
        """
        # 构建完整内容（正文不带标题，避免标题相似度主导召回）
        title = video.title or "未知标题"
        content_parts: List[str] = []

        if video.content and video.content.strip():
            content_parts.append(video.content.strip())

        # 如果有分段提纲，添加结构化信息
        if video.outline:
            outline_text = "\n## 内容提纲\n"
            for item in video.outline:
                item_title = item.get('title', '') or ''
                outline_text += f"\n### {item_title}\n"
                for point in item.get("points", []):
                    point_content = point.get('content', '') or ''
                    if point_content:
                        outline_text += f"- {point_content}\n"
            if outline_text.strip() != "## 内容提纲":
                content_parts.append(outline_text)

        full_content = "\n\n".join(content_parts).strip()

        # 验证内容不为空
        if not full_content or len(full_content.strip()) < 10:
            trace_logger.warning(f"[{video.bvid}] 内容太少，跳过")
            return []

        # 分块
        chunks = self.text_splitter.split_text(full_content)

        if not chunks:
            trace_logger.warning(f"[{video.bvid}] 没有生成文档块")
            return []

        # 过滤空内容块
        valid_chunks = [c for c in chunks if c and c.strip() and len(c.strip()) > 5]
        if not valid_chunks:
            trace_logger.warning(f"[{video.bvid}] 没有有效的文档块")
            return []

        # 创建文档。额外加入一条元信息文档，提升标题/简介/UP主类问题召回率。
        documents: List[Document] = []
        metadata_doc = self._build_metadata_document(video)
        if metadata_doc:
            documents.append(metadata_doc)

        for i, chunk in enumerate(valid_chunks):
            doc = Document(
                page_content=chunk.strip(),  # 确保是干净的字符串
                metadata={
                    "bvid": video.bvid,
                    "title": title,
                    "source": video.source.value,
                    "platform": video.platform,
                    "doc_type": "chunk",
                    "chunk_index": i,
                    "url": _build_video_url(video.platform, video.bvid)
                }
            )
            documents.append(doc)
        return documents

    def add_video_content(
        self,
        video: VideoContent,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """
        添加单个视频内容到向量库

        Args:
            video: VideoContent 对象

        Returns:
            添加的文档块数量
        """
        documents = self.prepare_documents(video)
        if not documents:
            return 0

        # 添加到向量库
        added_ids: List[str] = []
        try:
            batch_size = 10
            for idx in range(0, len(documents), batch_size):
                ensure_not_cancelled(cancel_check)
                added_ids.extend(self.vectorstore.add_documents(documents[idx:idx + batch_size]))
                ensure_not_cancelled(cancel_check)
            trace_logger.info(f"[{video.bvid}] 添加了 {len(documents)} 个文档块")
        except OperationCancelled:
            if added_ids:
                try:
                    self.vectorstore._collection.delete(ids=added_ids)
                except Exception as cleanup_error:
                    trace_logger.error(f"[{video.bvid}] 取消后清理向量失败: {cleanup_error}")
                    _record_pending_cleanup(video.bvid, video.platform, added_ids, "cancel_cleanup_failed")
            raise
        except Exception as e:
            trace_logger.error(f"[{video.bvid}] 添加到向量库失败: {e}")
            if added_ids:
                try:
                    self.vectorstore._collection.delete(ids=added_ids)
                    trace_logger.warning(f"[{video.bvid}] 已清理 {len(added_ids)} 个未完成向量")
                except Exception as cleanup_error:
                    trace_logger.error(f"[{video.bvid}] 清理未完成向量失败: {cleanup_error}")
                    _record_pending_cleanup(video.bvid, video.platform, added_ids, "compensation_failed")
            raise

        # 向量入库成功后写入 FTS5 索引（混合检索关键词召回通道）。
        # 失败仅记录日志，不影响向量库主流程（检索时 FTS5 不可用会自动降级纯向量）。
        self._upsert_fts_index(video)

        return len(documents)

    @staticmethod
    def _build_fts_content(video: VideoContent) -> str:
        """拼接 FTS5 可搜索文本：标题 + 简介 + 转写 + 提纲标题。

        与 prepare_documents 的 full_content 保持一致语义，但合并为单条文本
        供 BM25 全文索引（不需要切片）。
        """
        parts: List[str] = []
        if video.title:
            parts.append(video.title)
        if video.description:
            parts.append(video.description)
        if video.content and video.content.strip():
            parts.append(video.content.strip())
        if video.outline:
            outline_titles = []
            for item in video.outline:
                if isinstance(item, dict):
                    title = (item.get("title") or "").strip()
                    if title:
                        outline_titles.append(title)
            if outline_titles:
                parts.append("；".join(outline_titles[:8]))
        return "\n".join(p for p in parts if p).strip()

    def _upsert_fts_index(self, video: VideoContent) -> None:
        """向 FTS5 索引写入/更新一条视频记录。失败仅记录日志。"""
        try:
            from app.services.hybrid_retriever import get_hybrid_retriever
            content = self._build_fts_content(video)
            if not content:
                return
            get_hybrid_retriever().upsert(video.bvid, content)
        except Exception as e:
            trace_logger.warning(f"[{video.bvid}] FTS5 upsert 失败（不影响向量入库）: {e}")

    def _remove_fts_index(self, bvid: str) -> None:
        """从 FTS5 索引删除一条视频记录。失败仅记录日志。"""
        try:
            from app.services.hybrid_retriever import get_hybrid_retriever
            get_hybrid_retriever().remove(bvid)
        except Exception as e:
            trace_logger.warning(f"[{bvid}] FTS5 remove 失败（不影响向量删除）: {e}")
    
    # add_videos_batch 并行化参数
    BATCH_MAX_WORKERS = 5  # 同时处理的视频数上限（与 Semaphore=5 对齐）

    def add_videos_batch(self, videos: List[VideoContent], progress_callback=None) -> dict:
        """
        批量添加视频到向量库（并行化版本）。

        实现：ThreadPoolExecutor + 计数信号量，默认最多同时处理 BATCH_MAX_WORKERS=5 个视频。
        进度回调用 threading.Lock 保证线程安全，callback(current, total, title) 中
        current 单调递增但步长不一定为 1（哪个线程先完成先回调）。

        Args:
            videos: VideoContent 列表
            progress_callback: 进度回调 callback(current, total, title)

        Returns:
            {"success": 成功数, "failed": 失败数, "chunks": 总块数}
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(videos)
        if total == 0:
            return {"success": 0, "failed": 0, "chunks": 0}

        success = 0
        failed = 0
        total_chunks = 0
        processed_counter = 0
        counter_lock = threading.Lock()

        def _process_one(video: VideoContent) -> tuple[bool, int, str]:
            """处理单个视频，返回 (是否成功, chunks数量, 视频标题)。"""
            try:
                chunks = self.add_video_content(video)
                return True, chunks, video.title
            except Exception as e:
                trace_logger.error(f"添加视频失败 [{video.bvid}]: {e}")
                return False, 0, video.title

        workers = min(self.BATCH_MAX_WORKERS, max(1, total))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_video = {
                executor.submit(_process_one, v): v for v in videos
            }
            for fut in as_completed(future_to_video):
                ok, chunks, title = fut.result()
                with counter_lock:
                    if ok:
                        success += 1
                        total_chunks += chunks
                    else:
                        failed += 1
                    processed_counter += 1
                    current = processed_counter
                if progress_callback:
                    try:
                        progress_callback(current, total, title)
                    except Exception as cb_err:
                        trace_logger.warning(f"add_videos_batch 回调异常: {cb_err}")

        return {
            "success": success,
            "failed": failed,
            "chunks": total_chunks,
        }
    
    def search(
        self,
        query: str,
        k: int = 5,
        bvids: Optional[List[str]] = None,
        platform: Optional[str] = None,
        fetch_k: Optional[int] = None,
        use_mmr: bool = True,
    ) -> List[Document]:
        """
        检索相关内容

        当 settings.hybrid_search_enabled=True 时，向量召回后叠加 FTS5 BM25 关键词
        召回通道，两路结果用 RRF 融合后返回。FTS5 失败时 logger.warning 降级为纯
        向量检索（不影响主流程）。
        """
        if not query or not query.strip():
            trace_logger.warning("检索查询为空")
            return []

        try:
            requested_k = max(1, k)
            candidate_k = max(fetch_k or settings.retrieval_mmr_fetch_k, requested_k)
            search_filter = None
            conditions = []
            if bvids:
                conditions.append({"bvid": {"$in": bvids}})
            if platform:
                conditions.append({"platform": platform})
            if len(conditions) == 1:
                search_filter = conditions[0]
            elif len(conditions) > 1:
                search_filter = {"$and": conditions}
            docs: List[Document] = []

            if use_mmr:
                try:
                    docs = self.vectorstore.max_marginal_relevance_search(
                        query,
                        k=requested_k,
                        fetch_k=candidate_k,
                        lambda_mult=settings.retrieval_mmr_lambda,
                        filter=search_filter,
                    )
                except Exception as e:
                    trace_logger.warning(f"MMR 检索失败，降级 similarity_search: {e}")

            if not docs:
                if search_filter:
                    docs = self.vectorstore.similarity_search(query, k=requested_k, filter=search_filter)
                else:
                    docs = self.vectorstore.similarity_search(query, k=requested_k)

            # 混合检索：FTS5 BM25 召回 + RRF 融合
            docs = self._maybe_hybrid_rerank(query, docs, requested_k)

            trace_logger.info(f"检索完成：query='{query}'，召回={len(docs)}")
            for idx, doc in enumerate(docs):
                meta = doc.metadata or {}
                title = meta.get("title", "")
                bvid = meta.get("bvid", "")
                chunk_index = meta.get("chunk_index", "")
                preview = doc.page_content[:120].replace("\n", " ").strip()
                trace_logger.info(f"召回[{idx+1}] {bvid} #{chunk_index} {title} | {preview}")

            return docs
        except Exception as e:
            trace_logger.error(f"向量检索失败: {e}")
            raise RuntimeError("向量检索失败") from e

    def _maybe_hybrid_rerank(
        self, query: str, vector_docs: List[Document], top_n: int
    ) -> List[Document]:
        """对向量召回结果叠加 FTS5 BM25 RRF 重排。

        - settings.hybrid_search_enabled=False：直接返回原向量结果
        - FTS5 通道初始化或检索异常：logger.warning 后返回原向量结果
        - 正常：两路 video_id 用 RRF 融合，仅保留有 doc 的项（FTS-only 命中
          因缺少 chunk 内容会被丢弃，避免给下游传无 page_content 的占位文档）

        Args:
            query: 原始查询文本
            vector_docs: 向量召回的 Document 列表（已按相关度排序）
            top_n: 最终返回数量上限

        Returns:
            重排后的 Document 列表（长度不超过 top_n）
        """
        if not vector_docs:
            return vector_docs
        if not getattr(settings, "hybrid_search_enabled", True):
            return vector_docs[:top_n]
        try:
            from app.services.hybrid_retriever import (
                get_hybrid_retriever, rrf_fusion
            )
            retriever = get_hybrid_retriever()
        except Exception as e:
            trace_logger.warning(f"FTS5 通道不可用，降级纯向量检索: {e}")
            return vector_docs[:top_n]
        try:
            fts_results = retriever.search(query, top_n=top_n)
        except Exception as e:
            trace_logger.warning(f"FTS5 检索失败，降级纯向量检索: {e}")
            return vector_docs[:top_n]
        if not fts_results:
            return vector_docs[:top_n]

        # 把向量召回结果转为 RRF 输入格式（video_id + doc 引用）
        vector_dicts = []
        doc_index: dict[str, Document] = {}
        for doc in vector_docs:
            bvid = (doc.metadata or {}).get("bvid", "")
            if not bvid:
                continue
            vector_dicts.append({"video_id": bvid, "doc": doc})
            # 同一 bvid 可能多个 chunk，仅保留首个用于融合后回填
            doc_index.setdefault(bvid, doc)
        if not vector_dicts:
            return vector_docs[:top_n]

        fts_dicts = [{"video_id": r["video_id"], "score": r["score"]} for r in fts_results]
        fused = rrf_fusion(vector_dicts, fts_dicts, k=60, top_n=top_n)

        # 从融合结果回填 Document；FTS-only 命中（无 doc）跳过
        reranked: List[Document] = []
        for item in fused:
            doc = item.get("doc")
            if isinstance(doc, Document):
                # 在 metadata 上记录融合分数，便于下游观测
                try:
                    meta = dict(doc.metadata or {})
                    meta["rrf_score"] = item.get("rrf_score")
                    doc.metadata = meta
                except Exception:
                    pass
                reranked.append(doc)
        if not reranked:
            return vector_docs[:top_n]
        return reranked

    def get_collection_stats(self) -> dict:
        """
        获取向量库统计信息
        
        Returns:
            统计信息字典
        """
        try:
            collection = self.vectorstore._collection
            count = collection.count()
            
            # 获取唯一视频数
            result = collection.get(include=["metadatas"])
            bvids = set()
            for meta in result.get("metadatas", []):
                if meta and "bvid" in meta:
                    bvids.add(meta["bvid"])
            
            return {
                "total_chunks": count,
                "total_videos": len(bvids),
                "collection_name": self.collection_name
            }
        except Exception as e:
            trace_logger.error(f"获取统计信息失败: {e}")
            raise RuntimeError(f"获取统计信息失败: {e}") from e
    
    def has_video(self, bvid: str) -> bool:
        """检查指定视频是否实际存在于向量库。"""
        try:
            result = self.vectorstore._collection.get(where={"bvid": bvid}, limit=1)
            return bool(result.get("ids"))
        except Exception as e:
            trace_logger.error(f"查询视频向量失败 [{bvid}]: {e}")
            raise RuntimeError(f"查询视频向量失败 [{bvid}]") from e

    def has_videos(self, bvids: list[str]) -> dict[str, bool]:
        """批量查询多个视频是否存在于向量库。

        相比逐个调用 has_video，可减少 N 次 Chroma RPC，对大收藏夹提速明显。
        返回 {bvid: bool} 字典；查询异常的 bvid 视为 False。
        """
        if not bvids:
            return {}
        result_map: dict[str, bool] = {bvid: False for bvid in bvids}
        try:
            # 使用 $in 批量查询 bvid，减少 N 次 RPC
            collection = self.vectorstore._collection
            # 分批避免单次 get 返回过多数据
            BATCH = 200
            for i in range(0, len(bvids), BATCH):
                batch = bvids[i:i + BATCH]
                try:
                    res = collection.get(where={"bvid": {"$in": batch}})
                    metadatas = res.get("metadatas") or []
                    # collection.get 返回所有匹配的文档，每个 chunk 一行
                    # 任何一个 metadata.bvid 命中即视为存在
                    present_bvids: set[str] = set()
                    for meta in metadatas:
                        if meta and "bvid" in meta:
                            present_bvids.add(meta["bvid"])
                    for bvid in batch:
                        if bvid in present_bvids:
                            result_map[bvid] = True
                except Exception as batch_err:
                    trace_logger.warning(f"批量查询向量失败 (batch {i}): {batch_err}")
                    # 该批失败则逐个降级
                    for bvid in batch:
                        try:
                            single = collection.get(where={"bvid": bvid}, limit=1)
                            if single.get("ids"):
                                result_map[bvid] = True
                        except Exception:
                            pass
            return result_map
        except Exception as e:
            trace_logger.error(f"批量查询视频向量失败: {e}")
            # 整体异常时降级为逐个查询
            for bvid in bvids:
                try:
                    single = self.vectorstore._collection.get(where={"bvid": bvid}, limit=1)
                    if single.get("ids"):
                        result_map[bvid] = True
                except Exception:
                    pass
            return result_map

    def get_all_video_ids(self) -> list[str]:
        """获取向量库中所有不重复的视频 bvid 列表。"""
        try:
            result = self.vectorstore._collection.get(include=["metadatas"])
            bvid_set: set[str] = set()
            for meta in result.get("metadatas", []):
                if meta and "bvid" in meta:
                    bvid_set.add(meta["bvid"])
            return sorted(bvid_set)
        except Exception as e:
            trace_logger.error(f"获取所有视频ID失败: {e}")
            raise RuntimeError(f"获取所有视频ID失败: {e}") from e

    def count_video_chunks(self, bvid: str) -> int:
        """统计指定视频在向量库中的 chunk 数量。"""
        try:
            result = self.vectorstore._collection.get(where={"bvid": bvid})
            return len(result.get("ids", []))
        except Exception as e:
            trace_logger.error(f"统计视频chunk数量失败 [{bvid}]: {e}")
            raise RuntimeError(f"统计视频chunk数量失败 [{bvid}]: {e}") from e

    def delete_video(self, bvid: str):
        """
        删除指定视频的所有文档块

        Args:
            bvid: 视频 BV 号
        """
        try:
            self.vectorstore._collection.delete(where={"bvid": bvid})
            trace_logger.info(f"已删除视频: {bvid}")
        except Exception as e:
            trace_logger.error(f"删除视频失败 [{bvid}]: {e}")
            raise
        # 同步清理 FTS5 索引（best-effort，失败不阻塞向量删除流程）
        self._remove_fts_index(bvid)


# ---------------------------------------------------------------------------
# 待清理向量记录辅助函数（写 pending_cleanup 表，供 DataSyncer 优先处理）
# ---------------------------------------------------------------------------

def _record_pending_cleanup(
    bvid: str,
    platform: Optional[str],
    vector_ids: List[str],
    reason: str = "compensation_failed",
) -> None:
    """将补偿删除失败的向量 ID 写入 pending_cleanup 表。

    使用 sqlite3 直写（同步），避免 asyncio.run_coroutine_threadsafe
    在线程/非事件循环上下文中抛错。写入失败仅记录日志，
    DataSyncer 孤儿向量检测会兜底清理。
    """
    try:
        import json
        import sqlite3
        from datetime import datetime, timezone
        from app.config import settings

        url = settings.database_url
        if not url.startswith("sqlite"):
            return  # 仅 SQLite 实现（生产用 PostgreSQL 时这里按需扩展）
        path = url.split(":///")[-1] if ":///" in url else ""
        if url.startswith("sqlite+aiosqlite:////"):
            path = "/" + url.split(":////", 1)[1]
        elif url.startswith("sqlite:////"):
            path = "/" + url.split("////", 1)[1]
        elif url.startswith("sqlite+aiosqlite:///"):
            path = url.split(":///", 1)[1]
        elif url.startswith("sqlite:///"):
            path = url.split(":///", 1)[1]
        if not path:
            return

        conn = sqlite3.connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_cleanup (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bvid VARCHAR(32) NOT NULL,
                    platform VARCHAR(20),
                    vector_ids_json TEXT NOT NULL,
                    reason VARCHAR(200) DEFAULT 'compensation_failed',
                    created_at DATETIME,
                    cleaned BOOLEAN DEFAULT 0,
                    cleaned_at DATETIME
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pending_cleanup_bvid ON pending_cleanup (bvid)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_pending_cleanup_platform ON pending_cleanup (platform)"
            )
            now = utcnow()
            conn.execute(
                """
                INSERT INTO pending_cleanup
                (bvid, platform, vector_ids_json, reason, created_at, cleaned)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    bvid,
                    platform,
                    json.dumps(list(vector_ids)),
                    reason,
                    now,
                ),
            )
            conn.commit()
            trace_logger.info(
                f"已记录 pending_cleanup: bvid={bvid} vectors={len(vector_ids)} reason={reason}"
            )
        finally:
            conn.close()
    except Exception as e:
        trace_logger.error(f"写入 pending_cleanup 失败 bvid={bvid}: {e}")
