"""Embedding serialization and cosine distance."""

from __future__ import annotations

import numpy as np


def pack_embedding(values: list[float]) -> bytes:
    array = np.asarray(values, dtype=np.float32)
    if array.shape != (384,):
        raise ValueError(f"Expected 384-dimensional embedding, got shape {array.shape}")
    return array.tobytes()


def unpack_embedding(blob: bytes) -> list[float]:
    array = np.frombuffer(blob, dtype=np.float32)
    return array.astype(np.float64).tolist()


def cosine_distance(left: list[float] | np.ndarray, right: list[float] | np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 1.0
    return 1.0 - float(np.dot(a, b) / denom)
