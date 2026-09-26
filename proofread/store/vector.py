"""Numpy cosine index behind the VectorIndex port, plus a deterministic local text embedding.

No embedding API is available among the project keys, so `embed` is a deterministic feature-hashing
embedding: lowercased word unigrams and bigrams plus character 3-grams, each hashed (blake2b) to one of
DIM buckets with a hash-derived sign, sublinear TF weighting, then L2 normalization. Similar intents
(sharing words and word pieces) get high cosine similarity. Same text always yields the same vector,
across processes and machines.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import numpy as np

DIM = 512
_TOKEN = re.compile(r"[a-z0-9_]+")


def _bucket(feature: str) -> tuple[int, float]:
    h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    v = int.from_bytes(h, "little")
    return v % DIM, (1.0 if (v >> 63) & 1 else -1.0)


def embed(text: str) -> list[float]:
    text = (text or "").lower()
    toks = _TOKEN.findall(text)
    counts: dict[str, float] = {}
    for t in toks:
        counts["w:" + t] = counts.get("w:" + t, 0) + 1
    for a, b in zip(toks, toks[1:]):
        k = f"b:{a} {b}"
        counts[k] = counts.get(k, 0) + 1
    for t in toks:
        padded = f"#{t}#"
        for i in range(len(padded) - 2):
            k = "c:" + padded[i : i + 3]
            counts[k] = counts.get(k, 0) + 0.5
    vec = np.zeros(DIM, dtype=np.float64)
    for feat, c in counts.items():
        idx, sign = _bucket(feat)
        vec[idx] += sign * (1.0 + math.log(c)) if c >= 1 else sign * c
    n = float(np.linalg.norm(vec))
    if n > 0:
        vec /= n
    return vec.tolist()


class NumpyVectorIndex:
    """Cosine top-k over an in-memory matrix. Optionally persisted into a Store collection.

    Persistence layout (MongoDB-friendly, maps to an Atlas Vector Search collection later):
    collection "vectors", doc id f"{namespace}:{doc_id}", fields namespace, doc_id, vector, meta.
    """

    COLLECTION = "vectors"

    def __init__(self, store: Any = None, namespace: str = "default") -> None:
        self.store = store
        self.namespace = namespace
        self._ids: list[str] = []
        self._meta: list[dict[str, Any]] = []
        self._vecs: list[np.ndarray] = []
        self._pos: dict[str, int] = {}
        if store is not None:
            for d in store.find(self.COLLECTION, {"namespace": namespace}):
                self._add_local(d["doc_id"], d["vector"], d.get("meta") or {})

    def __len__(self) -> int:
        return len(self._ids)

    def _add_local(self, doc_id: str, vector: list[float], meta: dict[str, Any]) -> None:
        v = np.asarray(vector, dtype=np.float64)
        n = float(np.linalg.norm(v))
        v = v / n if n > 0 else v
        if doc_id in self._pos:
            i = self._pos[doc_id]
            self._vecs[i], self._meta[i] = v, meta
            return
        self._pos[doc_id] = len(self._ids)
        self._ids.append(doc_id)
        self._vecs.append(v)
        self._meta.append(meta)

    def add(self, doc_id: str, vector: list[float], meta: dict[str, Any] | None = None) -> None:
        meta = dict(meta or {})
        self._add_local(doc_id, vector, meta)
        if self.store is not None:
            self.store.put(self.COLLECTION, f"{self.namespace}:{doc_id}",
                           {"namespace": self.namespace, "doc_id": doc_id, "vector": list(map(float, vector)),
                            "meta": meta})

    def search(self, vector: list[float], k: int = 5) -> list[tuple[str, float, dict[str, Any]]]:
        if not self._ids or k <= 0:
            return []
        q = np.asarray(vector, dtype=np.float64)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        sims = np.vstack(self._vecs) @ q
        order = np.argsort(-sims, kind="stable")[:k]
        return [(self._ids[i], float(sims[i]), self._meta[i]) for i in order]
