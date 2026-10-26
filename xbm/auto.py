"""增量流水线:新书签 → 补嵌入 → 类目/双分区自动归位(全程零 LLM,毫秒级)。

设计依据:簇分区本身就是"覆盖内容最广的自动标签体系"(LLM 只在簇诞生时命名一次),
新书签用嵌入向量对簇心做余弦归位;类目同理复用持久化的类目向量。
"""

from . import category, cluster, embed


def classify_new(conn, embedder=None) -> dict:
    """同步后的自动处理。返回统计;嵌入失败不阻断归位(已有向量照常处理)。"""
    out = {"embedded": 0, "category": 0,
           "kw": 0, "kw_unmatched": 0, "llm": 0, "llm_unmatched": 0}
    pending = conn.execute(
        "SELECT COUNT(*) FROM tweet WHERE embedding IS NULL AND embed_text != ''"
    ).fetchone()[0]
    if pending:
        try:
            emb = embedder or embed.get_embedder("cpu")
            out["embedded"] = embed.ensure_embeddings_local(conn, embedder=emb)
        except Exception as e:  # noqa: BLE001 - 无本地嵌入器时留给页面/手动批嵌
            out["embed_error"] = f"{type(e).__name__}: {e}"
    out["category"] = category.assign_missing(conn)
    out["kw"], out["kw_unmatched"] = cluster.assign_missing(conn, "keyword")
    out["llm"], out["llm_unmatched"] = cluster.assign_missing(conn, "llm")
    return out
