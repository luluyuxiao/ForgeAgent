"""Optional OpenAI-compatible embeddings for Tool Catalog retrieval."""

from __future__ import annotations

from openai import OpenAI


class OpenAICompatibleEmbedder:
    """Small synchronous adapter around an OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
    ):
        if not model:
            raise ValueError("embedding model must not be empty")
        if not api_key:
            raise ValueError("embedding api_key must not be empty")
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(model=self.model, input=texts)
        rows = sorted(response.data, key=lambda item: item.index)
        if len(rows) != len(texts):
            raise ValueError("embedding provider returned an unexpected result count")
        return [[float(value) for value in item.embedding] for item in rows]
