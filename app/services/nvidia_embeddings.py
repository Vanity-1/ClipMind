"""NVIDIA NIM embeddings adapter -- adds required input_type field."""
import os
from typing import List
import httpx
from openai import OpenAI


class NVIDIAEmbeddings:
    """LangChain-compatible embed_documents / embed_query for NVIDIA NIM.

    NVIDIA nv-embedqa-e5-v5 requires an extra "input_type" field:
    - "passage" for document indexing
    - "query" for search queries
    """

    def __init__(
        self,
        model: str = "nvidia/nv-embedqa-e5-v5",
        api_key: str = "",
        base_url: str = "https://integrate.api.nvidia.com/v1",
    ): 
        self.model = model
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy, trust_env=False) if proxy else None
        self.client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    def embed_documents(self, texts: List[str]) -> List[List[float]]: 
        """Embed documents for indexing -- input_type=passage."""
        return self._embed(texts, "passage")

    def embed_query(self, text: str) -> List[float]: 
        """Embed a query for search -- input_type=query."""
        return self._embed([text], "query")[0]

    def _embed(self, texts: List[str], input_type: str) -> List[List[float]]: 
        resp = self.client.embeddings.create(
            model=self.model,
            input=texts,
            extra_body={"input_type": input_type},
        )
        return [d.embedding for d in resp.data]
