"""Ideas like this: similar ideas, and search by meaning from typed text.

Idea Machine embeds every idea (title, a newline, the statement) with BAAI/bge-small-en-v1.5, unit length, long
texts mean-pooled over 250-word windows. Lattice embeds a query the same way with fastembed's ONNX build of the
model, which reproduces Idea Machine's vectors exactly, and asks pub.ideas_near for the closest ideas. If Idea
Machine's model ever changes (pub.embedding_model()), search by meaning turns off rather than compare unlike vectors.
"""

import os
import threading

import psycopg

MODEL = "BAAI/bge-small-en-v1.5"
MODEL_VERSION = f"{MODEL};window=250;mean"   # Idea Machine's name for how its idea vectors were made
WINDOW = 250

_lock = threading.Lock()
_model = None
MISSING = (psycopg.errors.UndefinedFunction, psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege)


class Unavailable(Exception):
    """Search by meaning can't run: no model here, or Idea Machine doesn't publish it (yet), or its model differs."""


def _embedder():
    global _model
    with _lock:
        if _model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise Unavailable("search by meaning isn't installed here") from None
            _model = TextEmbedding(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
        return _model


def embed(text: str) -> list[float]:
    import numpy as np

    words = str(text).split()
    chunks = [" ".join(words[i:i + WINDOW]) for i in range(0, max(len(words), 1), WINDOW)]
    vecs = np.array(list(_embedder().embed(chunks)))
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    v = vecs.mean(axis=0)
    return (v / np.linalg.norm(v)).tolist()


def near(conn, text: str, k: int = 10) -> list[tuple[str, float]]:
    """The k ideas closest in meaning to `text`, closest first, as (idea id, cosine similarity)."""
    if not str(text).strip():
        return []
    try:
        model = conn.execute("SELECT pub.embedding_model()").fetchone()[0]
    except MISSING:
        raise Unavailable("Idea Machine doesn't publish search by meaning yet") from None
    if model != MODEL_VERSION:
        raise Unavailable(f"Idea Machine now embeds with {model}; Lattice knows {MODEL_VERSION}")
    vec = "[" + ",".join(f"{x:.6f}" for x in embed(text)) + "]"
    try:
        return [(str(i), float(s)) for i, s in conn.execute(
            "SELECT idea_id, similarity FROM pub.ideas_near(%s::vector, %s)", (vec, max(1, min(int(k), 50))))]
    except psycopg.errors.InvalidParameterValue:   # a vector of another size: the models differ after all
        raise Unavailable("Idea Machine's vectors are a different size") from None


def neighbours(conn) -> dict:
    """Each idea's closest others, as Idea Machine publishes them: {idea id: [[other id, similarity], ...]}."""
    try:
        rows = conn.execute("SELECT idea_id, other_id, similarity FROM pub.idea_neighbours ORDER BY idea_id, rank")
        out: dict = {}
        for i, other, sim in rows:
            out.setdefault(str(i), []).append([str(other), round(float(sim), 3)])
        return out
    except MISSING:
        return {}
