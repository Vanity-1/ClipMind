"""混合检索：SQLite FTS5 BM25 + 向量 + RRF 融合

本模块提供独立的 FTS5 BM25 关键词召回通道，与现有 ChromaDB 向量召回通过
RRF (Reciprocal Rank Fusion) 融合。FTS5 虚拟表与主 SQLite 数据库同库，
原生持久化，无需额外文件或服务。

设计要点：
- jieba 可选：缺失时回退空格分词（中文召回率下降但不报错）
- FTS5 表用原生 sqlite3 连接，绕过 SQLAlchemy ORM 复杂性
- rrf_fusion 是纯函数，不依赖外部状态，便于单元测试
- HybridRetriever 实例通过 get_hybrid_retriever() 模块级单例获取
"""
import sqlite3
import threading
from typing import List, Optional

from loguru import logger

# jieba 可选依赖：中文搜索引擎模式分词，召回率优于纯空格切分
try:
    import jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

_RRF_K = 60  # RRF 常数：score = 1/(k+rank)，常用 60


def _tokenize(text: str) -> str:
    """分词：jieba.cut_for_search（搜索引擎模式，召回率高），缺失时回退空格。

    FTS5 默认 tokenizer 是空格/标点切分，对中文无词边界识别能力，因此入库前
    显式分词后写入。jieba.cut_for_search 会再切出细粒度子词，提升 BM25 召回。
    """
    if not text:
        return ""
    if _HAS_JIEBA:
        return " ".join(jieba.cut_for_search(text))
    return " ".join(text.split())


def _resolve_sqlite_path(database_url: str) -> str:
    """从 SQLAlchemy database_url 中提取 SQLite 文件路径。

    支持 sqlite:///、sqlite+aiosqlite:///、绝对路径（四斜杠）等变体。
    与 app/services/rag.py._record_pending_cleanup 中保持一致的解析逻辑。
    """
    if not database_url or not database_url.startswith("sqlite"):
        return ""
    if database_url.startswith("sqlite+aiosqlite:////"):
        return "/" + database_url.split(":////", 1)[1]
    if database_url.startswith("sqlite:////"):
        return "/" + database_url.split("////", 1)[1]
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url.split(":///", 1)[1]
    if database_url.startswith("sqlite:///"):
        return database_url.split(":///", 1)[1]
    return ""


class HybridRetriever:
    """FTS5 BM25 检索 + RRF 融合通道。

    FTS5 虚拟表与主 SQLite DB 同库，原生持久化（与应用数据放一起，备份/迁移
    时无需额外处理）。表结构：
        CREATE VIRTUAL TABLE video_fts USING fts5(content, video_id UNINDEXED)
    - content：分词后的可搜索文本（jieba.cut_for_search 输出）
    - video_id UNINDEXED：仅作为关联键，不参与全文索引（避免 bvid 噪声）

    线程安全：每个 upsert/remove/search 调用都新建短连接，无共享状态。
    SQLite 默认的连接级锁足够覆盖单进程内的并发写入。
    """

    def __init__(self, db_path: str):
        if not db_path:
            raise ValueError("HybridRetriever 需要 db_path（SQLite 文件路径）")
        self.db_path = db_path
        self._init_fts_table()

    def _connect(self) -> sqlite3.Connection:
        """新建 sqlite3 连接。FTS5 在大多数发行版 SQLite 中默认编译启用。"""
        conn = sqlite3.connect(self.db_path)
        # 启用 WAL 后兼容性：与主库 PRAGMA journal_mode=WAL 一致，并发更友好
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as e:
            logger.debug(f"[HybridRetriever] PRAGMA 设置跳过: {e}")
        return conn

    def _init_fts_table(self) -> None:
        """创建 FTS5 虚拟表（content/video_id 两列），若已存在则跳过。

        content 列存分词后的文本，video_id 标记为 UNINDEXED 不参与全文索引
        （仅作为外部关联键，避免 bvid 字符串污染 BM25 评分）。
        """
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS video_fts "
                    "USING fts5(content, video_id UNINDEXED)"
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            # FTS5 未编译进 SQLite 时会在此抛错，调用方应感知降级
            logger.error(f"[HybridRetriever] 创建 FTS5 表失败: {e}")
            raise

    def upsert(self, video_id: str, content: str) -> None:
        """增量 upsert：先删除该 video_id 旧记录，再插入分词后的新记录。

        Args:
            video_id: 视频唯一标识（与向量库 metadata.bvid 对齐）
            content: 原始可搜索文本（标题/简介/转写拼接，未分词）
        """
        if not video_id:
            return
        tokenized = _tokenize(content or "")
        if not tokenized.strip():
            # 空内容仍要清理旧记录，避免 stale 文本残留
            self.remove(video_id)
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM video_fts WHERE video_id = ?", (video_id,)
                )
                conn.execute(
                    "INSERT INTO video_fts(content, video_id) VALUES(?, ?)",
                    (tokenized, video_id),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[HybridRetriever] upsert 失败 video_id={video_id}: {e}")

    def remove(self, video_id: str) -> None:
        """删除指定 video_id 的 FTS5 记录。"""
        if not video_id:
            return
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM video_fts WHERE video_id = ?", (video_id,)
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[HybridRetriever] remove 失败 video_id={video_id}: {e}")

    def search(self, query: str, top_n: int = 10) -> List[dict]:
        """BM25 检索，返回 [{video_id, score}] 按 bm25 分数降序。

        FTS5 的 bm25() 函数返回值越小越相关（通常为负数），
        这里取负值并向上取整为可比较的"分数越大越相关"语义，方便调用方统一处理。

        Args:
            query: 原始查询文本（未分词）
            top_n: 返回结果数上限

        Returns:
            [{"video_id": str, "score": float}]，score 越大越相关
        """
        if not query or not query.strip() or top_n <= 0:
            return []
        tokenized_query = _tokenize(query)
        if not tokenized_query.strip():
            return []
        # FTS5 MATCH 表达式对特殊字符敏感，用双引号包裹整体作为短语查询
        # 避免 OR/AND/NOT 等 FTS5 操作符被误解析
        match_expr = '"' + tokenized_query.replace('"', '""') + '"'
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT video_id, bm25(video_fts) AS score "
                    "FROM video_fts WHERE video_fts MATCH ? "
                    "ORDER BY score ASC LIMIT ?",
                    (match_expr, int(top_n)),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[HybridRetriever] search 失败 query='{query[:50]}': {e}")
            return []
        # bm25() 返回负数（越小越相关），取负后变成"越大越相关"
        return [{"video_id": row[0], "score": -float(row[1])} for row in rows]


def rrf_fusion(
    vector_docs: List[dict],
    fts_docs: List[dict],
    k: int = _RRF_K,
    top_n: int = 4,
) -> List[dict]:
    """RRF (Reciprocal Rank Fusion) 融合：score = Σ 1/(k+rank)。

    两路文档的 video_id 相同则合并分数（不同通道的同一视频叠加权重）。
    rank 从 1 开始计数（首位 rank=1，score=1/(k+1)）。

    Args:
        vector_docs: 向量召回结果，每个 dict 至少含 "video_id"，
            可携带其他字段（如 "doc"）会在融合后保留
        fts_docs: FTS5 召回结果，每个 dict 至少含 "video_id"
        k: RRF 常数，默认 60
        top_n: 返回结果数上限

    Returns:
        融合后的文档列表，按 rrf_score 降序。每个 dict 保留原字段并新增
        "rrf_score" 字段。两路 video_id 重叠时字段合并（vector 侧优先，
        FTS 侧补充缺失字段）。
    """
    if top_n <= 0:
        return []

    scores: dict[str, float] = {}
    merged: dict[str, dict] = {}
    best_rank: dict[str, int] = {}

    def _accumulate(docs: List[dict], channel: str) -> None:
        for rank, doc in enumerate(docs, start=1):
            vid = doc.get("video_id") if isinstance(doc, dict) else None
            if not vid:
                continue
            vid = str(vid)
            scores[vid] = scores.get(vid, 0.0) + 1.0 / (k + rank)
            best_rank[vid] = min(best_rank.get(vid, rank), rank)
            existing = merged.get(vid)
            if existing is None:
                # 浅拷贝避免修改调用方传入的 dict
                merged[vid] = dict(doc)
                merged[vid]["video_id"] = vid
            else:
                # 字段合并：已存在的字段优先（vector 侧先累积），缺失的补齐
                for key, value in doc.items():
                    if key not in existing:
                        existing[key] = value

    # vector 侧先累积，保证 "doc" 等关键字段在合并时优先保留
    _accumulate(vector_docs, "vector")
    _accumulate(fts_docs, "fts")

    if not scores:
        return []

    ordered = sorted(
        scores,
        key=lambda vid: (-scores[vid], best_rank.get(vid, 10_000), vid),
    )

    results: List[dict] = []
    for vid in ordered[:top_n]:
        out = merged[vid]
        out["rrf_score"] = round(scores[vid], 6)
        results.append(out)
    return results


# ---------------------------------------------------------------------------
# 模块级单例与工厂
# ---------------------------------------------------------------------------

_retriever_instance: Optional[HybridRetriever] = None
_singleton_lock = threading.Lock()


def get_hybrid_retriever() -> HybridRetriever:
    """获取 HybridRetriever 模块级单例。

    首次调用时从 app.config.settings.database_url 解析 SQLite 文件路径并初始化
    FTS5 表。后续调用直接返回缓存实例（避免每次重新建表/连接）。

    失败时（如 FTS5 未编译、database_url 非 SQLite）抛出异常给调用方，
    由调用方决定降级策略（RAGService.search 会 logger.warning 后回退纯向量）。
    """
    global _retriever_instance
    if _retriever_instance is not None:
        return _retriever_instance
    with _singleton_lock:
        if _retriever_instance is not None:
            return _retriever_instance
        from app.config import settings
        db_path = _resolve_sqlite_path(getattr(settings, "database_url", ""))
        if not db_path:
            raise RuntimeError(
                "无法从 settings.database_url 解析 SQLite 路径，"
                "HybridRetriever 仅支持 SQLite 后端"
            )
        _retriever_instance = HybridRetriever(db_path)
        logger.info(f"[HybridRetriever] 单例已初始化 db_path={db_path}")
        return _retriever_instance


def reset_hybrid_retriever() -> None:
    """重置模块级单例（仅供测试使用，使下次 get_hybrid_retriever 重新初始化）。"""
    global _retriever_instance
    with _singleton_lock:
        _retriever_instance = None
