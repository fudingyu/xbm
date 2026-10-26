"""2D 地图投影:UMAP 主路径(装了 umap-learn),numpy-SVD PCA 兜底(零依赖)。

坐标归一化到 0..1 存 tweet.map_x/map_y,前端 canvas 直接渲染。
"""

import sqlite3
import time
from dataclasses import dataclass

import numpy as np

from . import embed


@dataclass
class MapStats:
    points: int = 0
    method: str = ""
    seconds: float = 0.0
    error: str = ""


def _pca_2d(M: np.ndarray) -> np.ndarray:
    """零依赖兜底:中心化 + SVD 取前两主成分。"""
    X = M - M.mean(axis=0, keepdims=True)
    # 右奇异向量:Vt 的前两行
    _, _, vt = np.linalg.svd(X, full_matrices=False)
    return X @ vt[:2].T


def compute_map(conn: sqlite3.Connection, method: str = "umap",
                on_phase=None) -> MapStats:
    def phase(label):
        if on_phase and label:
            on_phase(label)

    stats = MapStats()
    ids, M = embed.load_matrix(conn)
    stats.points = len(ids)
    if not ids:
        stats.error = "无已嵌入书签"
        return stats
    t0 = time.time()
    try:
        if method != "pca":
            phase("UMAP 降维中(首次约 1-2 分钟)")
            import umap
            coords = umap.UMAP(
                n_components=2, n_neighbors=15, min_dist=0.1,
                random_state=42, verbose=False,
            ).fit_transform(M)
            stats.method = "umap"
        else:
            raise ImportError  # 走兜底
    except ImportError:
        phase("PCA 降维中(未装 umap-learn,兜底)")
        coords = _pca_2d(M)
        stats.method = "pca"
    except Exception as e:  # noqa: BLE001 - UMAP 失败退 PCA
        phase(f"UMAP 失败({e}),退 PCA")
        coords = _pca_2d(M)
        stats.method = "pca"

    # 归一化到 0..1(等比,保持形状)
    mins, maxs = coords.min(axis=0), coords.max(axis=0)
    span = np.where(maxs - mins > 1e-9, maxs - mins, 1.0)
    norm = (coords - mins) / span
    conn.executemany(
        "UPDATE tweet SET map_x=?, map_y=? WHERE tweet_id=?",
        [(float(x), float(y), tid) for tid, (x, y) in zip(ids, norm)])
    conn.commit()
    stats.seconds = round(time.time() - t0, 1)
    return stats


def map_points(conn: sqlite3.Connection, color: str = "category") -> dict:
    """前端散点数据:{points:[{id,x,y,g}], groups:{g:label}};g=null 未分组。"""
    col = {"category": "category", "kw": "cluster_keyword",
           "llm": "cluster_llm"}.get(color, "category")
    points, groups = [], {}
    if col == "category":
        for name, in conn.execute(
                "SELECT DISTINCT category FROM tweet "
                "WHERE category IS NOT NULL ORDER BY category"):
            groups[len(groups)] = name
        gmap = {name: i for i, name in groups.items()}
        rows = conn.execute(
            "SELECT tweet_id, map_x, map_y, category, text FROM tweet "
            "WHERE map_x IS NOT NULL")
        for tid, x, y, cat, text in rows:
            points.append({"id": tid, "x": round(x, 4), "y": round(y, 4),
                           "g": gmap.get(cat), "t": (text or "")[:60]})
    else:
        set_name = "keyword" if col == "cluster_keyword" else "llm"
        for cid, name in conn.execute(
                "SELECT cluster_id, COALESCE(name,'未命名') FROM cluster "
                "WHERE set_name=?", (set_name,)):
            groups[cid] = name
        rows = conn.execute(
            f"SELECT tweet_id, map_x, map_y, {col}, text FROM tweet "
            "WHERE map_x IS NOT NULL")
        for tid, x, y, cid, text in rows:
            points.append({"id": tid, "x": round(x, 4), "y": round(y, 4),
                           "g": cid, "t": (text or "")[:60]})
    return {"points": points, "groups": groups, "color": color}
