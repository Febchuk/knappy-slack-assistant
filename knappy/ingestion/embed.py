"""Local 384-dimensional embeddings.

Uses fastembed when KNAPPY_EMBEDDER=fastembed and the package is installed.
Otherwise builds a deterministic unit vector so ingestion works offline.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np

_MODEL = None
_FASTEMBED_FAILED = False


def _hashed_unit_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little")
    vector = np.random.default_rng(seed).standard_normal(384).astype(np.float64)
    norm = np.linalg.norm(vector)
    if norm == 0.0:
        vector[0] = 1.0
        norm = 1.0
    return (vector / norm).tolist()


def _fastembed(text: str) -> list[float] | None:
    global _MODEL, _FASTEMBED_FAILED
    if os.environ.get("KNAPPY_EMBEDDER") != "fastembed" or _FASTEMBED_FAILED:
        return None
    try:
        if _MODEL is None:
            from fastembed import TextEmbedding

            _MODEL = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        return list(_MODEL.embed([text]))[0].tolist()
    except Exception:
        _FASTEMBED_FAILED = True
        return None


def generate_embedding(text: str) -> list[float]:
    embedded = _fastembed(text)
    if embedded is not None:
        return embedded
    return _hashed_unit_vector(text)
