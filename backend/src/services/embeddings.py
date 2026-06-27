"""Embedding service for semantic deduplication via cosine similarity clustering."""

from __future__ import annotations

import logging
import math
from typing import Any

from src.services.llm import llm_service

logger = logging.getLogger(__name__)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def compute_embeddings(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts."""
    if not texts:
        return []
    return await llm_service.embed(texts)


async def find_semantic_duplicates(
    items: list[dict[str, Any]],
    text_key: str = "text",
    threshold: float = 0.92,
) -> list[list[int]]:
    """Find groups of semantically duplicate items.

    Returns list of groups, where each group is a list of indices
    into the original items list. Items with cosine similarity > threshold
    are grouped together.
    """
    texts = [item.get(text_key, "") for item in items]
    if not texts:
        return []

    embeddings = await compute_embeddings(texts)

    # Union-Find for clustering
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Compare all pairs (O(n^2) but n is typically < 100)
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            sim = cosine_similarity(embeddings[i], embeddings[j])
            if sim > threshold:
                union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        root = find(i)
        groups.setdefault(root, []).append(i)

    # Only return groups with duplicates (size > 1)
    return [indices for indices in groups.values() if len(indices) > 1]
