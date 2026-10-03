"""Local 384-dimensional embeddings (Spec 13 §5).

fastembed with bge-small-en-v1.5 is the default. KNAPPY_EMBEDDER=hash selects a deterministic
hashed vector for tests. If fastembed cannot load, embeddings are None and callers rank by text
alone; random vectors are never passed off as meaning.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger("knappy")

MODEL_NAME = "BAAI/bge-small-en-v1.5"

_model: Any = None
_failed = False


def _hashed_unit_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little")
    vector = np.random.default_rng(seed).standard_normal(384).astype(np.float64)
    return (vector / np.linalg.norm(vector)).tolist()


def _load() -> Any:
    global _model, _failed
    if _model is None and not _failed:
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(model_name=MODEL_NAME)
        except Exception as exc:
            _failed = True
            logger.warning("embedder unavailable, ranking by text only: %s: %s", type(exc).__name__, exc)
    return _model


def semantic() -> bool:
    """True when embeddings carry meaning, so cosine similarity is worth ranking on."""
    return os.environ.get("KNAPPY_EMBEDDER") != "hash" and _load() is not None


def generate_embedding(text: str, *, query: bool = False) -> list[float] | None:
    """Embed a stored passage, or a search query (bge adds its retrieval instruction to queries)."""
    if os.environ.get("KNAPPY_EMBEDDER") == "hash":
        return _hashed_unit_vector(text)
    model = _load()
    if model is None:
        return None
    vectors = model.query_embed([text]) if query else model.passage_embed([text])
    return [float(value) for value in next(iter(vectors))]
