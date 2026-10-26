"""向量存取(BLOB float32)+ 可选的服务端嵌入客户端。

主路线:浏览器 WebGPU(@litert-lm/core)算好向量回传 /api/embeddings;
备选:CLI `embed` 走 OpenAI 兼容 /v1/embeddings(litert_lm serve 或 llama.cpp)。
"""

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def vec_to_blob(vec) -> bytes:
    return normalize(vec).astype(np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def pending_rows(conn: sqlite3.Connection, limit: int = 512) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT tweet_id, embed_text FROM tweet "
        "WHERE embedding IS NULL AND embed_text != '' ORDER BY rowid LIMIT ?",
        (limit,),
    ).fetchall()


def store_embeddings(conn: sqlite3.Connection, items: list[tuple[str, list]] , model: str) -> int:
    """items: [(tweet_id, vector)];已存在的 embedding 不覆盖。"""
    n = 0
    for tid, vec in items:
        cur = conn.execute(
            "UPDATE tweet SET embedding=?, embedding_model=?, embedded_at=? "
            "WHERE tweet_id=? AND embedding IS NULL",
            (vec_to_blob(vec), model, _now(), tid),
        )
        n += cur.rowcount
    conn.commit()
    return n


def load_matrix(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    rows = conn.execute(
        "SELECT tweet_id, embedding FROM tweet WHERE LENGTH(embedding) > 0"
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 1), dtype=np.float32)
    ids, vecs = [], []
    for r in rows:
        v = blob_to_vec(r["embedding"])
        if len(v) == 0:  # 防御:异常空向量不入矩阵
            continue
        ids.append(r["tweet_id"])
        vecs.append(v)
    if not vecs:
        return [], np.zeros((0, 1), dtype=np.float32)
    return ids, np.vstack(vecs)


def coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    total = conn.execute("SELECT COUNT(*) FROM tweet").fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM tweet WHERE LENGTH(embedding) > 0").fetchone()[0]
    return done, total


class LocalEmbedder:
    """进程内 litert_lm 嵌入:backend="cpu"(XNNPACK,实测 ~8.8 条/s)或
    "gpu"(WebGPU,实测 ~14.7 条/s,初始化稍慢)。批量建库与类目嵌入主路径。"""

    # 模型输入上限 1024 token;中文约 1 字/词 1 token,留出前缀余量
    MAX_CHARS = 850

    def __init__(self, model_path: str | None = None, backend: str = "cpu"):
        from litert_lm import Backend, EmbeddingEngine  # 延迟导入,未装包时再报错
        self.path = model_path or config.discover_model()
        if not self.path or not Path(self.path).is_file():
            raise FileNotFoundError(config.MODEL_NOT_FOUND_HINT)
        self.backend = backend
        bk = Backend.GPU() if backend == "gpu" else Backend.CPU()
        self.model = EmbeddingEngine(self.path, backend=bk)
        self.name = Path(self.path).stem
        r = self.model.compute_embedding_batch(["warmup"])  # 预热 + 探测维度
        self.dim = len(r[0].embedding)

    def embed(self, texts: list[str]) -> list:
        """返回与输入等长的列表:成功项为向量,失败项为 None(截断+逐条降级)。"""
        cap = self.MAX_CHARS
        inputs = [config.DOC_PREFIX + (t or "")[:cap] for t in texts]
        try:
            resps = self.model.compute_embedding_batch(inputs)
            return [np.asarray(r.embedding, dtype=np.float32) for r in resps]
        except Exception:  # noqa: BLE001 - 个别文本炸批次时逐条降级
            out = []
            for x in inputs:
                try:
                    r = self.model.compute_embedding_batch([x])[0]
                    out.append(np.asarray(r.embedding, dtype=np.float32))
                except Exception:  # noqa: BLE001
                    out.append(None)
            return out

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        resps = self.model.compute_embedding_batch(
            [config.QUERY_PREFIX + (t or "")[:500] for t in texts])
        return np.asarray([r.embedding for r in resps], dtype=np.float32)


_embedder_cache: dict[str, LocalEmbedder] = {}


def get_embedder(backend: str = "cpu") -> LocalEmbedder:
    """按后端缓存引擎实例(初始化 CPU 0.2s / GPU ~5s,避免重复建)。"""
    if backend not in _embedder_cache:
        _embedder_cache[backend] = LocalEmbedder(backend=backend)
    return _embedder_cache[backend]


def ensure_embeddings_local(conn: sqlite3.Connection, embedder: LocalEmbedder | None = None,
                            batch: int = 128, on_batch=None) -> int:
    """Python 进程内批量嵌入全部缺失行(主路径);顽固失败行标记跳过。"""
    emb = embedder or get_embedder("cpu")
    total = failed = 0
    while True:
        rows = pending_rows(conn, limit=batch)
        if not rows:
            break
        results = emb.embed([r["embed_text"] or "" for r in rows])
        ok = [(r["tweet_id"], v) for r, v in zip(rows, results) if v is not None]
        if ok:
            total += store_embeddings(conn, ok, emb.name)
        bad = [r["tweet_id"] for r, v in zip(rows, results) if v is None]
        if bad:  # 空 BLOB 标记"已处理但失败",不再进 pending
            conn.executemany(
                "UPDATE tweet SET embedding=X'', embedding_model=? WHERE tweet_id=?",
                [(emb.name, tid) for tid in bad])
            conn.commit()
            failed += len(bad)
        if on_batch:
            on_batch(total)
    if failed:
        print(f"(跳过 {failed} 条无法嵌入的文本)")
    return total


class EmbedClient:
    """OpenAI 兼容 /v1/embeddings 客户端(urllib 零依赖,批量带重试)。"""

    def __init__(self, base: str | None = None, model: str | None = None, timeout: int | None = None):
        self.base = (base or config.EMBED_BASE).rstrip("/")
        self.model = model or config.EMBED_MODEL
        self.timeout = timeout or config.EMBED_TIMEOUT

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), config.EMBED_BATCH):
            batch = texts[i : i + config.EMBED_BATCH]
            req = urllib.request.Request(
                f"{self.base}/embeddings",
                data=json.dumps({"model": self.model, "input": batch}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last_err = None
            for _ in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))["data"]
                    order = {d["index"]: d["embedding"] for d in data}
                    vecs.extend(order[i] for i in range(len(batch)))
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001 - 统一重试
                    last_err = e
            if last_err:
                raise RuntimeError(f"嵌入服务请求失败({self.base}): {last_err}")
        return np.asarray(vecs, dtype=np.float32)


def ensure_embeddings(conn: sqlite3.Connection, client: EmbedClient, doc_prefix: str | None = None) -> int:
    """服务端批量嵌入所有缺失行(CLI 备选路径;主路径是浏览器回传)。"""
    prefix = doc_prefix if doc_prefix is not None else config.DOC_PREFIX
    total = 0
    while True:
        rows = pending_rows(conn, limit=config.EMBED_BATCH * 8)
        if not rows:
            break
        texts = [prefix + (r["embed_text"] or "") for r in rows]
        mat = client.embed(texts)
        total += store_embeddings(conn, list(zip([r["tweet_id"] for r in rows], mat)), client.model)
    return total
