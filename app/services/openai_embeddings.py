"""OpenAI 兼容 embeddings 适配器。

与 NVIDIAEmbeddings 对称，但不传 input_type 字段。
适用于 OpenAI 官方、Azure OpenAI、第三方 OpenAI 兼容 API。
"""
import os
from typing import List
import httpx
from openai import OpenAI


class OpenAICompatibleEmbeddings:
    """LangChain-compatible embed_documents / embed_query for OpenAI-compatible APIs."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
    ):
        self.model = model
        # 代理处理与生产 NVIDIAEmbeddings / 测试端点一致
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy, trust_env=False) if proxy else None
        self.client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Embed documents for indexing."""
        resp = self.client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> List[float]:
        """Embed a query for search."""
        resp = self.client.embeddings.create(model=self.model, input=[text])
        return resp.data[0].embedding
