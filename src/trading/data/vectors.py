"""Semantic memory: a small vector store over news, lessons, and past decisions so
agents can ask "have we seen this before, and what happened?"

Ships with a dependency-free local embedding (hashed bag-of-words -> unit vector) so
it is fully testable now. A production embedding model (OpenAI/Cohere/local
sentence-transformer) drops in behind `Embedder` with no change to callers — that
model is the M15 blocked dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_DIM = 256
_TOKEN = re.compile(r"[a-z0-9]+")


def _stable_bucket(token: str, dim: int) -> int:
    # Stable across processes (unlike built-in hash()), so persisted vectors still
    # match queries after a restart.
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % dim


class Embedder:
    """Interface for turning text into a fixed-length vector."""
    dim: int = _DIM

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(Embedder):
    """Local, deterministic, dependency-free embedding: hashed token counts,
    L2-normalized. Good enough for near-duplicate / similar-context recall; the
    OpenAI embedder below is the production upgrade when a key is present."""

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall((text or "").lower()):
            vec[_stable_bucket(tok, self.dim)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


class OpenAIEmbedder(Embedder):
    """Production semantic embeddings via OpenAI. Used only for vector memory —
    the trading agents themselves run on Claude/Anthropic. Selected automatically
    when OPENAI_API_KEY is set; construction fails loudly so the factory can fall
    back to the local embedder."""

    def __init__(self, model: str = "text-embedding-3-small", api_key: str | None = None):
        import os

        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.dim = 1536  # text-embedding-3-small

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=self.model, input=text or " ")
        return resp.data[0].embedding


def get_embedder(config=None) -> Embedder:
    """Return the best available embedder: OpenAI when a key is configured, else the
    dependency-free local hashing embedder (which always works, offline)."""
    import os

    if os.getenv("OPENAI_API_KEY"):
        try:
            return OpenAIEmbedder()
        except Exception:
            pass  # missing openai package / bad key -> local fallback
    return HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are unit vectors


@dataclass
class Recall:
    kind: str
    ref_id: str
    text: str
    score: float


class VectorStore:
    def __init__(self, db_path: str | Path, embedder: Embedder | None = None):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "id INTEGER PRIMARY KEY, kind TEXT, ref_id TEXT, text TEXT, vec TEXT, "
            "UNIQUE(kind, ref_id))")
        self.conn.commit()
        # OpenAI embeddings when a key is present, else the local fallback. All
        # vectors in one store must share an embedder (dimensions must match) — a
        # fresh store adopts whatever is available at creation time.
        self.embedder = embedder or get_embedder()

    def close(self) -> None:
        self.conn.close()

    def add(self, kind: str, ref_id: str, text: str) -> None:
        vec = json.dumps(self.embedder.embed(text))
        self.conn.execute(
            "INSERT INTO vectors (kind, ref_id, text, vec) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, ref_id) DO UPDATE SET text=excluded.text, vec=excluded.vec",
            (kind, ref_id, text, vec))
        self.conn.commit()

    def search(self, query: str, k: int = 5, kind: str | None = None) -> list[Recall]:
        qv = self.embedder.embed(query)
        rows = self.conn.execute(
            "SELECT * FROM vectors" + (" WHERE kind=?" if kind else ""),
            (kind,) if kind else ()).fetchall()
        scored = [Recall(kind=r["kind"], ref_id=r["ref_id"], text=r["text"],
                         score=round(cosine(qv, json.loads(r["vec"])), 4)) for r in rows]
        scored.sort(key=lambda r: r.score, reverse=True)
        return [s for s in scored[:k] if s.score > 0]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM vectors").fetchone()["n"])
