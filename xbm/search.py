"""混合检索:FTS5(trigram 中文子串)× 语义余弦 → RRF 融合,支持元数据过滤。"""

import sqlite3

import numpy as np

from . import config, embed

RRF_K = 60


def _candidate_rows(conn, author=None, before=None, after=None, has_media=None):
    sql = (
        "SELECT t.rowid AS rid, t.tweet_id, t.author_handle, t.author_name, t.text, "
        "       t.created_at, t.media_json, t.urls_json, "
        "       COALESCE(b.bookmarked_at, t.created_at) AS date_sort, "
        "       t.embedding IS NOT NULL AS has_vec "
        "FROM tweet t LEFT JOIN bookmark b ON b.tweet_id = t.tweet_id WHERE 1=1"
    )
    args: list = []
    if author:
        sql += " AND lower(t.author_handle) LIKE ?"
        args.append(f"%{author.lower().lstrip('@')}%")
    if before:
        sql += " AND COALESCE(b.bookmarked_at, t.created_at) <= ?"
        args.append(before)
    if after:
        sql += " AND COALESCE(b.bookmarked_at, t.created_at) >= ?"
        args.append(after)
    if has_media:
        sql += " AND json_array_length(t.media_json) > 0"
    return {r["rid"]: dict(r) for r in conn.execute(sql, args)}


def _fts_ranks(conn, query: str, rids: list[int], k: int):
    if not rids:
        return {}
    q = query.strip().replace('"', "")
    if len(q) < 3:  # trigram 需要 >=3 字符,短查询直接在主表 LIKE
        like = f"%{q}%"
        marks = ",".join("?" * len(rids))
        rows = conn.execute(
            f"SELECT rowid AS rid FROM tweet WHERE text LIKE ? AND rowid IN ({marks}) "
            "ORDER BY rowid LIMIT ?",
            [like, *rids, k],
        ).fetchall()
        return {r["rid"]: -i for i, r in enumerate(rows)}
    marks = ",".join("?" * len(rids))
    try:
        rows = conn.execute(
            f"SELECT rowid AS rid, bm25(tweet_fts) AS score FROM tweet_fts "
            f"WHERE tweet_fts MATCH ? AND rowid IN ({marks}) "
            "ORDER BY score LIMIT ?",
            [f'"{q}"', *rids, k],
        ).fetchall()
        return {r["rid"]: r["score"] for r in rows}
    except Exception:
        return {}


def _vec_ranks(conn, qvec, cand: dict, k: int):
    if not any(c["has_vec"] for c in cand.values()):
        return {}, None
    if qvec is None:
        return {}, "无查询向量(前端未传且服务端无嵌入服务)"
    all_ids, mat = embed.load_matrix(conn)
    if not all_ids:
        return {}, "库中无任何向量,先完成嵌入"
    idx = {tid: i for i, tid in enumerate(all_ids)}
    q = embed.normalize(qvec)
    rows = []
    for c in cand.values():
        tid = c["tweet_id"]
        if c["has_vec"] and tid in idx:
            rows.append((c["rid"], float(mat[idx[tid]] @ q)))
    rows.sort(key=lambda x: -x[1])
    return {rid: (rank, sim) for rank, (rid, sim) in enumerate(rows[:k])}, None


def hybrid_search(conn: sqlite3.Connection, query: str, qvec=None, mode: str = "auto",
                  k: int = 20, author=None, before=None, after=None, has_media=None):
    """返回 (hits, note)。hits 按融合分排序;qvec 是已归一化的查询向量。"""
    cand = _candidate_rows(conn, author, before, after, has_media)
    if not cand:
        return [], "无候选书签(检查过滤条件或先运行同步)"

    fts = {} if mode == "vector" else _fts_ranks(conn, query, list(cand), k)
    vec, err = ({}, None) if mode == "fts" else _vec_ranks(conn, qvec, cand, k)

    hits = []
    fts_sorted = sorted(fts, key=lambda r: fts[r])[:k]
    vec_sorted = sorted(vec, key=lambda r: vec[r][0])[:k]
    for rid, c in cand.items():
        score = 0.0
        if rid in fts_sorted:
            score += 1.0 / (RRF_K + fts_sorted.index(rid) + 1)
        if rid in vec_sorted:
            score += 1.0 / (RRF_K + vec_sorted.index(rid) + 1)
        if score <= 0:
            continue
        c = dict(c)
        c.pop("has_vec", None)
        c["fts_hit"] = rid in fts
        c["sim"] = vec.get(rid, (None, None))[1]
        c["score"] = round(score, 6)
        hits.append(c)
    hits.sort(key=lambda h: -h["score"])
    note = err or ""
    if mode != "fts" and not qvec:
        note = note or "未提供查询向量,本次仅关键词检索"
    return hits[:k], note


def embed_query_text(query: str) -> str:
    return config.QUERY_PREFIX + query
