"""本地 ONNX Embedding 推理（不依赖 torch / sentence-transformers）。

设计背景
--------
安装包为减小体积（约 1.5GB），排除了 torch / sentence-transformers /
transformers 等大型 ML 库。为让本地向量模型（bge-small-zh 等）在打包环境下
可用，引入 ONNX Runtime 推理路径：

- 模型权重：HF 仓库的 onnx/model_quantized.onnx（int8 量化，~24MB）
- 分词器：模型目录内的 tokenizer.json（tokenizers 库，纯 Python）
- 池化：CLS token + L2 归一化（与 sentence-transformers 的 bge 默认配置一致）

LangChain 兼容接口：embed_documents / embed_query。
"""
import os
from typing import List, Optional

import numpy as np

# onnxruntime / tokenizers 为延迟导入：打包环境二者均已内置，
# 但开发者环境可能缺 onnxruntime，此时应给出明确提示而非裸 ImportError。
try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在缺依赖时触发
    ort = None
    _ORT_AVAILABLE = False

try:
    from tokenizers import Tokenizer
    _TOKENIZERS_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在缺依赖时触发
    Tokenizer = None
    _TOKENIZERS_AVAILABLE = False


# ONNX 模型可能的输入/输出名（不同导出工具命名不同）
_INPUT_NAMES = ("input_ids", "attention_mask", "token_type_ids")
_MAX_SEQ_LEN = 512


class OnnxRuntimeUnavailableError(RuntimeError):
    """onnxruntime 不可用时抛出，调用方据此回退到其他推理后端。"""


class LocalOnnxEmbeddings:
    """基于 ONNX Runtime 的本地向量模型推理。

    支持 bge 系列（CLS pooling + L2 归一化）。
    model_dir 需包含：
      - tokenizer.json
      - onnx/model.onnx 或 onnx/model_quantized.onnx
    """

    def __init__(
        self,
        model_dir: str,
        model_name: Optional[str] = None,
        max_seq_length: int = _MAX_SEQ_LEN,
    ):
        if not _ORT_AVAILABLE:
            raise OnnxRuntimeUnavailableError(
                "当前环境缺少 onnxruntime，无法使用本地 ONNX Embedding。"
                "请安装 onnxruntime 或切换 Embedding Provider 为 API 模式。"
            )
        if not _TOKENIZERS_AVAILABLE:
            raise OnnxRuntimeUnavailableError(
                "当前环境缺少 tokenizers，无法使用本地 ONNX Embedding。"
            )
        self.model_dir = model_dir
        self.model_name = model_name or os.path.basename(model_dir.rstrip("/\\"))
        self.max_seq_length = max_seq_length

        # 1. 分词器
        tok_path = os.path.join(model_dir, "tokenizer.json")
        if not os.path.exists(tok_path):
            raise FileNotFoundError(
                f"本地向量模型缺少 tokenizer.json：{model_dir}"
            )
        self._tokenizer = Tokenizer.from_file(tok_path)

        # 2. ONNX 权重：优先量化版（体积小、速度快），回退 fp32
        onnx_path = self._find_onnx_weight(model_dir)
        if not onnx_path:
            raise FileNotFoundError(
                f"本地向量模型缺少 ONNX 权重（onnx/model.onnx 或 "
                f"onnx/model_quantized.onnx）：{model_dir}"
            )
        self.onnx_path = onnx_path
        self._session = ort.InferenceSession(
            onnx_path,
            providers=["CPUExecutionProvider"],
        )

        # 3. 输入/输出名适配（不同导出工具命名可能不同）
        self._input_names = [i.name for i in self._session.get_inputs()]
        self._output_name = self._session.get_outputs()[0].name
        self._hidden_size = self._session.get_outputs()[0].shape[-1]
        self.embedding_dim = self._hidden_size

    @staticmethod
    def _find_onnx_weight(model_dir: str) -> Optional[str]:
        """在模型目录中查找 ONNX 权重，优先量化版。"""
        onnx_dir = os.path.join(model_dir, "onnx")
        candidates = []
        if os.path.isdir(onnx_dir):
            candidates = [
                os.path.join(onnx_dir, "model_quantized.onnx"),
                os.path.join(onnx_dir, "model.onnx"),
                os.path.join(onnx_dir, "model_int8.onnx"),
            ]
        # 兼容 onnx 权重直接放模型根目录的情况
        candidates += [
            os.path.join(model_dir, "model.onnx"),
            os.path.join(model_dir, "model_quantized.onnx"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _tokenize(self, texts: List[str]):
        """将文本批量编码为模型输入 dict（numpy int64）。"""
        encoded = self._tokenizer.encode_batch(
            texts,
            add_special_tokens=True,
            is_pretokenized=False,
        )
        input_ids_list, attention_mask_list = [], []
        for enc in encoded:
            ids = enc.ids[: self.max_seq_length]
            mask = [1] * len(ids)
            pad_len = self.max_seq_length - len(ids)
            if pad_len > 0:
                ids = ids + [0] * pad_len
                mask = mask + [0] * pad_len
            input_ids_list.append(ids)
            attention_mask_list.append(mask)

        feeds = {}
        for name in self._input_names:
            if name == "input_ids":
                feeds[name] = np.array(input_ids_list, dtype=np.int64)
            elif name == "attention_mask":
                feeds[name] = np.array(attention_mask_list, dtype=np.int64)
            elif name == "token_type_ids":
                feeds[name] = np.zeros_like(
                    np.array(input_ids_list, dtype=np.int64)
                )
            else:
                # 未知输入：忽略（ONNX 侧若有默认值可省略）
                pass
        return feeds

    def _embed(self, texts: List[str]) -> np.ndarray:
        """推理 + CLS 池化 + L2 归一化，返回 (n, dim) float32。"""
        feeds = self._tokenize(texts)
        output = self._session.run([self._output_name], feeds)[0]
        # CLS token 向量：last_hidden_state[:, 0]
        cls_vec = output[:, 0, :]
        norm = np.linalg.norm(cls_vec, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (cls_vec / norm).astype(np.float32)

    # ------------------------------------------------------------------
    # LangChain 兼容接口
    # ------------------------------------------------------------------
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量编码文档（用于入库）。"""
        if not texts:
            return []
        emb = self._embed(texts)
        return [row.tolist() for row in emb]

    def embed_query(self, text: str) -> List[float]:
        """编码单条查询。"""
        emb = self._embed([text])
        return emb[0].tolist()
