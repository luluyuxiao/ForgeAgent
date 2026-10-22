"""Small offline retrieval primitives shared by local indexes."""

from __future__ import annotations

import math
import re
from collections import Counter

_SEARCH_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", re.IGNORECASE)


def tokenize(value: str) -> list[str]:
    """Split English identifiers and CJK text into deterministic search tokens."""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).replace("_", " ")
    return [token.casefold() for token in _SEARCH_TOKEN.findall(expanded)]


def bm25_scores(query: str, documents: dict[str, str]) -> dict[str, float]:
    """Return positive BM25 scores for an in-memory document collection."""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return {}
    tokenized = {name: tokenize(text) for name, text in documents.items()}
    average_length = sum(len(tokens) for tokens in tokenized.values()) / len(tokenized)
    document_frequency = Counter(
        token for tokens in tokenized.values() for token in set(tokens)
    )
    total = len(tokenized)
    scores = {}
    for name, tokens in tokenized.items():
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if frequency == 0:
                continue
            frequency_in_documents = document_frequency[token]
            inverse_frequency = math.log(
                1
                + (total - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            normalization = frequency + 1.5 * (
                0.25 + 0.75 * len(tokens) / max(average_length, 1.0)
            )
            score += inverse_frequency * frequency * 2.5 / normalization
        if score > 0:
            scores[name] = score
    return scores
