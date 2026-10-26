"""零样本类目分类:类目描述向量 × 书签向量 → 余弦 + softmax(沿用 embed_decide_server 范式)。

类目描述的向量由浏览器 WebGPU 算好回传(/api/categories/embed),本模块只做 numpy 分配;
向量持久化在 meta('category_vecs'),新书签增量分配(assign_missing)直接复用,无需重嵌。
"""

import json
import math
import sqlite3
from dataclasses import dataclass

import numpy as np

from . import embed

# (名称, 语义描述)——描述写具体一点,零样本命中率更高
DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("AI与LLM", "大语言模型、AI 应用、Agent 智能体、提示词技巧、推理成本、训练微调、开源模型"),
    ("编程开发", "编程语言、框架、工程实践、开源项目、代码、开发调试、系统设计"),
    ("工具与效率", "软件工具、浏览器插件、自动化脚本、效率方法、工作流、笔记知识管理"),
    ("硬件与数码", "手机、电脑、显卡、芯片、DIY 硬件、消费电子、评测拆解"),
    ("科普与学习", "科学知识、数学、历史、论文解读、认知心理学、学习方法"),
    ("商业与投资", "创业、商业分析、公司战略、股票基金、加密货币、宏观经济"),
    ("新闻时事", "社会新闻、国际政治、行业动态、公众事件"),
    ("生活与娱乐", "影视、游戏、音乐、段子、体育、明星八卦"),
    ("旅行与美食", "旅行攻略、目的地推荐、美食、探店"),
    ("健康与运动", "健身、跑步、医疗健康、睡眠、饮食营养"),
    ("设计与创作", "UI 设计、平面设计、摄影、绘画、写作、视频创作"),
    ("其他", "不属于以上任何类目的杂项内容"),
]

TEMPERATURE = 0.08  # 余弦量程约 [-1,1],与 decide server 同款


@dataclass
class ClassifyStats:
    assigned: int = 0
    skipped: int = 0  # 无向量的书签
    distribution: dict = None

    def __post_init__(self):
        if self.distribution is None:
            self.distribution = {}


def assign_categories(conn: sqlite3.Connection, cat_vecs: list[tuple[str, list]]) -> ClassifyStats:
    """cat_vecs: [(类目名, 已归一化向量)];对全部已嵌入书签分配 top-1 类目。
    类目向量同时持久化到 meta('category_vecs'),供增量分配复用。"""
    names = [n for n, _ in cat_vecs]
    C = np.vstack([embed.normalize(v) for _, v in cat_vecs]).astype(np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('category_vecs', ?)",
        (json.dumps({n: [float(x) for x in v] for n, v in zip(names, C)},
                    ensure_ascii=False),))

    stats = ClassifyStats()
    rows = [
        (r[0], r[1]) for r in conn.execute(
            "SELECT tweet_id, embedding FROM tweet WHERE LENGTH(embedding) > 0")
        if len(r[1]) > 0
    ]
    if not rows:
        return stats
    M = np.vstack([embed.blob_to_vec(b) for _, b in rows])

    sims = M @ C.T                      # (n, k) 余弦(两侧已归一化)
    exps = np.exp(sims / TEMPERATURE)
    probs = exps / exps.sum(axis=1, keepdims=True)
    top = probs.argmax(axis=1)

    updates = []
    for i, r in enumerate(rows):
        name = names[int(top[i])]
        updates.append((name, float(probs[i, top[i]]), r[0]))
        stats.distribution[name] = stats.distribution.get(name, 0) + 1

    conn.executemany(
        "UPDATE tweet SET category=?, category_score=? WHERE tweet_id=?", updates)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('categories', ?)",
        (",".join(names),))
    conn.commit()
    stats.assigned = len(updates)
    return stats


def category_counts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n FROM tweet "
        "WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC").fetchall()
    return [{"name": r["category"], "count": r["n"]} for r in rows]


def unclassified(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM tweet WHERE LENGTH(embedding) > 0 AND category IS NULL"
    ).fetchone()[0]


def _stored_vecs(conn) -> dict[str, np.ndarray] | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='category_vecs'").fetchone()
    if not row:
        return None
    d = json.loads(row[0])
    return {k: np.asarray(v, dtype=np.float32) for k, v in d.items()}


def assign_missing(conn: sqlite3.Connection) -> int:
    """增量:仅给"已嵌入但未分类"的新书签分配类目(毫秒级,复用已存类目向量)。"""
    vecs = _stored_vecs(conn)
    if not vecs:
        return 0
    names = list(vecs)
    C = np.vstack([embed.normalize(vecs[n]) for n in names]).astype(np.float32)
    rows = [(r[0], r[1]) for r in conn.execute(
        "SELECT tweet_id, embedding FROM tweet "
        "WHERE LENGTH(embedding) > 0 AND category IS NULL")]
    if not rows:
        return 0
    M = np.vstack([embed.blob_to_vec(b) for _, b in rows])
    top = (M @ C.T).argmax(axis=1)
    conn.executemany(
        "UPDATE tweet SET category=? WHERE tweet_id=?",
        [(names[int(t)], tid) for (tid, _), t in zip(rows, top)])
    conn.commit()
    return len(rows)
