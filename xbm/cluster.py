"""聚类 + 簇命名:HDBSCAN 优先(装了就用),引领聚类兜底(零依赖);命名关键词自动、可选本地 LLM。

设计:全量 LLM 打标在本机算力下不现实(万条 × 秒级 = 天文数字),所以只让 LLM
给"簇"命名——调用量从几万次降到几十次(OpenAI 兼容 /v1/chat/completions,
如 llama.cpp 的 llama-server 或 litert_lm 的 chat 服务)。
"""

import json
import math
import re
import sqlite3
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from . import config, embed

ZH_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}")
EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.-]{2,}")
STOPWORDS = {
    "the", "and", "for", "you", "this", "that", "with", "from", "have", "will",
    "htt", "https", "www", "com",
    "关于", "一个", "我们", "可以", "这个", "就是", "还是", "但是", "如果", "不是",
    "他们", "自己", "什么", "没有", "觉得", "真的", "现在", "已经", "这样", "东西",
    "还能", "还可以", "这么", "怎么", "以及", "之后", "时候", "问题", "直接", "出来",
    "大家", "朋友", "东西", "地方", "事情", "一下", "一直", "起来", "下去", "看到",
    "时候", "今天", "昨天", "明天", "时候", "可能", "应该", "需要", "继续", "开始",
}


def _tokenize(text: str) -> set[str]:
    """中文 jieba 分词(词级),英文小写词;过滤停词。"""
    toks: set[str] = set()
    try:
        import jieba
        for w in jieba.cut(text, cut_all=False):
            w = w.strip()
            if len(w) >= 2 and "\u4e00" <= w[0] <= "\u9fff":
                toks.add(w)
    except ImportError:  # 无 jieba 时退化为中文 2+ 连字
        for w in ZH_TOKEN.findall(text):
            toks.add(w)
    for w in EN_TOKEN.findall(text):
        toks.add(w.lower())
    return {t for t in toks if t not in STOPWORDS}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ClusterStats:
    tweets: int = 0
    clusters: int = 0
    noise: int = 0
    method: str = ""
    named_by: str = ""


def leader_cluster(M: np.ndarray, threshold: float = 0.52) -> np.ndarray:
    """零依赖引领聚类:顺序扫描,与某中心余弦 ≥ threshold 则并入,否则自成中心。"""
    labels = np.full(len(M), -1, dtype=int)
    centroids: list[np.ndarray] = []
    for i in range(len(M)):
        best, best_sim = -1, threshold
        for ci, c in enumerate(centroids):
            sim = float(M[i] @ c)
            if sim > best_sim:
                best, best_sim = ci, sim
        if best < 0:
            centroids.append(M[i].copy())
            labels[i] = len(centroids) - 1
        else:
            labels[i] = best
            k = float(np.count_nonzero(labels == best))
            centroids[best] = centroids[best] * ((k - 1) / k) + M[i] / k  # 增量均值
    return labels


# 聚类粒度 → 阈值:两套方法各自的映射。
# LLM 分区 = 嵌入余弦引领聚类(Gemma 基线高,阈值 0.6+ 有效);
# 关键词分区 = 词集 Jaccard 引领聚类(完全不同的文本视角,与语义法正交)。
GRANULARITY = {
    "vfine": (0.70, "很细(很多小簇)"),
    "fine": (0.68, "细"),
    "medium": (0.65, "适中(默认)"),
    "coarse": (0.62, "粗(少量大簇)"),
}
GRANULARITY_KW = {
    "vfine": (0.14, "很细"),
    "fine": (0.12, "细"),
    "medium": (0.10, "适中(默认)"),
    "coarse": (0.09, "粗"),
}


def granularity_threshold(g: str, set_name: str = "llm") -> float:
    table = GRANULARITY_KW if set_name == "keyword" else GRANULARITY
    return table.get(g, table["medium"])[0]


def keyword_cluster(items: list[tuple[str, dict]], threshold: float) -> list[int]:
    """IDF 加权词向量引领聚类(文本视角,与嵌入聚类正交)。
    items: [(tweet_id, {token: 权重} 已归一化)];相似度 = 稀疏余弦;
    质心 = 成员增量均值后归一化。返回与 items 等长的簇标号(-1 = 特征不足)。"""
    labels = [-1] * len(items)
    cents: list[dict] = []
    for i, (_tid, v) in enumerate(items):
        if len(v) < 2:
            continue
        best, best_sim = -1, threshold
        for ci, c in enumerate(cents):
            if len(c) < len(v):
                s = sum(w * v.get(t, 0.0) for t, w in c.items())
            else:
                s = sum(w * c[t] for t, w in v.items() if t in c)
            if s > best_sim:
                best, best_sim = ci, s
        if best < 0:
            cents.append(dict(v))
            labels[i] = len(cents) - 1
        else:
            c = cents[best]
            for t, w in v.items():
                c[t] = c.get(t, 0.0) + w
            norm = math.sqrt(sum(x * x for x in c.values())) or 1.0
            for t in c:
                c[t] /= norm
            labels[i] = best
    return labels


def _try_hdbscan(M: np.ndarray, min_cluster_size: int):
    try:
        import hdbscan  # noqa: F401
        return hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size, min_samples=1,
            metric="euclidean", core_dist_n_jobs=-1,
        ).fit_predict(M)
    except Exception:  # noqa: BLE001 - 未装/数据不满足 → None
        return None


def run_clustering(conn: sqlite3.Connection, set_name: str = "keyword",
                   granularity: str = "medium",
                   threshold: float | None = None,
                   method: str = "auto", assign_threshold: float = 0.60,
                   min_cluster_size: int = 12,
                   on_phase=None) -> ClusterStats:
    """对指定分区独立聚类,两套方法正交,交集筛选才有叠加增益:
    - "keyword":词集加权 Jaccard 引领聚类(字面词汇视角);
    - "llm":    嵌入余弦引领聚类(语义视角)。
    簇心一律用嵌入质心(增量归位与地图着色统一)。
    granularity → 各方法自己的阈值表(GRANULARITY / GRANULARITY_KW)。"""
    def phase(label):
        if on_phase:
            on_phase(label)

    col = f"cluster_{set_name}"
    thr = threshold if threshold is not None else granularity_threshold(granularity, set_name)
    ids, M = embed.load_matrix(conn)
    stats = ClusterStats(tweets=len(ids), method="tokens-jaccard" if set_name == "keyword" else "embedding-leader")
    if not ids:
        return stats

    labels = np.full(len(M), -1, dtype=int)
    next_id = 0
    pending = np.arange(len(M))
    small = len(M) < 200  # 小语料走引领聚类(确定性、全覆盖)

    if set_name == "keyword":
        # ---- 方法一:IDF 加权词向量引领聚类(文本视角,与嵌入法正交) ----
        phase("分词与 IDF 向量化")
        glob: Counter = Counter()
        rows = conn.execute(
            "SELECT tweet_id, text FROM tweet WHERE LENGTH(embedding) > 0").fetchall()
        token_lists = [_tokenize(r["text"] or "") for r in rows]
        for toks in token_lists:
            glob.update(toks)
        n = len(rows)
        df_min = 1 if n < 500 else 3      # 小语料放宽,避免特征全灭
        df_max = n * 0.30                 # 过泛词不入特征
        items = []
        for r, toks in zip(rows, token_lists):
            v = {t: math.log(1.0 + n / glob[t])
                 for t in set(toks) if df_min <= glob[t] <= df_max}
            norm = math.sqrt(sum(w * w for w in v.values()))
            if norm > 0:
                v = {t: w / norm for t, w in v.items()}
            items.append((r["tweet_id"], v))
        phase("词向量引领聚类(文本视角)")
        lab = keyword_cluster(items, thr)
        sizes = Counter(l for l in lab if l >= 0)
        min_keep = max(1, min(min_cluster_size // 3, n // 10))  # 碎簇剪枝,随语料自适应
        remap = {}
        for l in sorted(sizes):
            if sizes[l] >= min_keep:
                remap[l] = len(remap)
        pos = {t: i for i, t in enumerate(ids)}
        for (tid, _), l in zip(items, lab):
            if l in remap:
                labels[pos[tid]] = remap[l]
        next_id = len(remap)
        stats.method = "tokens-jaccard"
        pending = pending[labels[pending] < 0]
    elif small:
        stats.method = "embedding-leader"
        lab = leader_cluster(M, 0.52)
        for f in sorted({int(x) for x in lab if x >= 0}):
            labels[np.where(lab == f)[0]] = next_id
            next_id += 1
        pending = pending[labels[pending] < 0]
    else:
        # 抽样引领聚类 + 全量质心归并。Gemma 系嵌入基线余弦高(无关文本 ~0.5-0.6),
        # 实测 threshold=0.65 在 4k 样本上给出 ~50 个可浏览簇;HDBSCAN 在此语料
        # 只会产生巨簇,弃用。
        phase("抽样建簇(约 4000 条)")
        rng = np.random.default_rng(42)
        sample_idx = rng.choice(len(M), size=min(4000, len(M)), replace=False)
        slab = leader_cluster(M[sample_idx], threshold=thr)
        stats.method = "embedding-leader"
        cents = []
        for f in sorted({int(x) for x in slab if x >= 0}):
            member_idx = np.where(slab == f)[0]
            if len(member_idx) < 20:
                continue  # 样本里的碎簇不要
            labels[sample_idx[member_idx]] = next_id
            next_id += 1
            cents.append(M[sample_idx[member_idx]].mean(axis=0))
        if cents:
            phase("全量质心归并")
            C = np.vstack(cents)
            C /= np.clip(np.linalg.norm(C, axis=1, keepdims=True), 1e-9, None)
            sims = M @ C.T
            best, best_sim = sims.argmax(axis=1), sims.max(axis=1)
            sample_labels = labels[sample_idx].copy()
            # 高于基线、低于建簇阈的归并;不像的少量保持 NULL
            labels = np.where(best_sim >= assign_threshold, best, -1)
            labels[sample_idx] = sample_labels  # 样本点保持抽样层给的簇
        pending = pending[labels[pending] < 0]

    # 残余:就近并入已有簇(余弦 ≥ 0.40),实在不像的保持 NULL。
    # 关键词分区跳过:嵌入兜底会破坏词集方法的纯粹性,未匹配留空即可。
    residual = np.where(labels < 0)[0]
    if set_name != "keyword" and len(residual) and next_id:
        cents = np.vstack([
            (lambda c: c / max(float(np.linalg.norm(c)), 1e-9))(M[labels == cid].mean(axis=0))
            for cid in range(next_id)
        ])
        sims = M[residual] @ cents.T
        best, best_sim = sims.argmax(axis=1), sims.max(axis=1)
        picked = np.where(best_sim >= 0.40, best, -1)
        labels[residual] = picked

    phase("写入库")
    conn.execute(f"UPDATE tweet SET {col}=NULL")  # 只清本分区
    bulk = [(int(lab) if lab >= 0 else None, tid) for tid, lab in zip(ids, labels)]
    conn.executemany(f"UPDATE tweet SET {col}=? WHERE tweet_id=?", bulk)
    stats.noise = sum(1 for _, lab in bulk if lab is None)
    stats.clusters = next_id

    # 簇元数据:规模 + 样本(离质心最近的几条,供命名/预览)+ 簇心(增量归位用)
    conn.execute("DELETE FROM cluster WHERE set_name=?", (set_name,))
    for cid in range(next_id):
        member_idx = np.where(labels == cid)[0]
        if not len(member_idx):
            continue
        sub = M[member_idx]
        centroid = sub.mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-9)
        order = np.argsort(-(sub @ centroid))[:8]
        sample = [ids[member_idx[j]] for j in order]
        conn.execute(
            "INSERT INTO cluster(set_name, cluster_id, name, size, sample_json, "
            "centroid_json, updated_at) VALUES(?,?,NULL,?,?,?,?)",
            (set_name, int(cid), len(member_idx),
             json.dumps(sample, ensure_ascii=False),
             json.dumps([float(x) for x in centroid]), _now()))
    conn.commit()
    return stats


def _centroids(conn, set_name: str) -> tuple[list[int], np.ndarray] | None:
    """读某分区簇心;缺失(老分区)则从成员向量自愈回填。"""
    rows = conn.execute(
        "SELECT cluster_id, centroid_json, size FROM cluster "
        "WHERE set_name=? ORDER BY size DESC", (set_name,)).fetchall()
    if not rows:
        return None
    if any(r["centroid_json"] is None for r in rows):
        backfill_centroids(conn, set_name)
        rows = conn.execute(
            "SELECT cluster_id, centroid_json FROM cluster "
            "WHERE set_name=? ORDER BY size DESC", (set_name,)).fetchall()
    cids = [r["cluster_id"] for r in rows]
    C = np.asarray([json.loads(r["centroid_json"]) for r in rows], dtype=np.float32)
    return cids, C


def backfill_centroids(conn, set_name: str) -> int:
    """老分区无簇心时,从成员向量现算回填(不重聚类、不动名字)。"""
    col = f"cluster_{set_name}"
    n = 0
    for r in conn.execute(
            "SELECT cluster_id FROM cluster WHERE set_name=?", (set_name,)):
        cid = r["cluster_id"]
        vecs = conn.execute(
            f"SELECT embedding FROM tweet WHERE {col}=? AND LENGTH(embedding)>0",
            (cid,)).fetchall()
        if not vecs:
            continue
        M = np.vstack([embed.blob_to_vec(v[0]) for v in vecs])
        c = M.mean(axis=0)
        c /= max(float(np.linalg.norm(c)), 1e-9)
        conn.execute(
            "UPDATE cluster SET centroid_json=? WHERE set_name=? AND cluster_id=?",
            (json.dumps([float(x) for x in c]), set_name, cid))
        n += 1
    conn.commit()
    return n


def assign_missing(conn, set_name: str, threshold: float = 0.60) -> tuple[int, int]:
    """增量归位:把"已嵌入但未进本分区"的新书签按簇心余弦归位(毫秒级,零 LLM)。
    返回 (归位数, 未匹配数);未匹配(< threshold)留空,攒多了再重聚类吸收。"""
    got = _centroids(conn, set_name)
    if got is None:
        return 0, 0
    cids, C = got
    col = f"cluster_{set_name}"
    rows = [(r[0], r[1]) for r in conn.execute(
        f"SELECT tweet_id, embedding FROM tweet "
        f"WHERE LENGTH(embedding) > 0 AND {col} IS NULL")]
    if not rows:
        return 0, 0
    M = np.vstack([embed.blob_to_vec(b) for _, b in rows])
    sims = M @ C.T
    best, best_sim = sims.argmax(axis=1), sims.max(axis=1)
    updates, unmatched = [], 0
    for (tid, _), cid, sim in zip(rows, best, best_sim):
        if sim >= threshold:
            updates.append((int(cid), tid))
        else:
            unmatched += 1
    if updates:
        conn.executemany(f"UPDATE tweet SET {col}=? WHERE tweet_id=?", updates)
        conn.commit()
    return len(updates), unmatched


def _cluster_texts(conn, sample_ids: list[str]) -> list[str]:
    out = []
    for tid in sample_ids:
        r = conn.execute("SELECT text FROM tweet WHERE tweet_id=?", (tid,)).fetchone()
        if r and r["text"]:
            out.append(r["text"][:200])
    return out


def name_clusters(conn: sqlite3.Connection, set_name: str = "keyword",
                  llm_base: str | None = None, llm_model: str | None = None,
                  style: str = "chat",
                  on_progress=None) -> tuple[int, str]:
    """给指定分区(set_name: "keyword"|"llm")补命名,只动该分区的 name 列,
    另一分区与已有名字互不影响。keyword 分区用关键词命名;llm 分区用 LLM
    (style=chat|systemone),LLM 无产出的簇留空待补。
    on_progress(i, total, preview): 命名进度回调。返回 (本次命名数, 方式)。"""
    use_llm = set_name == "llm"
    base = (llm_base or "").strip() or None
    model = (llm_model or "").strip() or None
    if use_llm and not base:
        raise ValueError("LLM 分区命名需要配置 API 地址")
    rows = conn.execute(
        "SELECT cluster_id, sample_json FROM cluster "
        "WHERE set_name=? AND name IS NULL ORDER BY size DESC",
        (set_name,)).fetchall()
    if not rows:
        return 0, "none"
    # 全局词频(关键词命名的区分度基准)
    glob = Counter()
    for (t,) in conn.execute("SELECT text FROM tweet WHERE text != ''"):
        glob.update(_tokenize(t))

    named = 0
    total = len(rows)
    used_names: set[str] = set()  # 批内去重:防小模型复读 few-shot 例子造成重名
    for i, r in enumerate(rows):
        texts = _cluster_texts(conn, json.loads(r["sample_json"]))
        # 簇关键词:关键词命名的素材,也是 systemone 决策的候选词
        freq = Counter()
        for (t,) in conn.execute(
                f"SELECT text FROM tweet WHERE cluster_{set_name}=?",
                (r["cluster_id"],)):
            freq.update(_tokenize(t))
        scored = [(w, c / (glob.get(w, 0) + 3)) for w, c in freq.most_common(60)]
        scored.sort(key=lambda x: -x[1])
        cands = [w for w, _ in scored[:12]]
        if on_progress:  # 在慢速 LLM 调用前上报(词频统计为毫秒级)
            on_progress(i + 1, total, "·".join(cands[:2]) or (texts[0][:16] if texts else ""))
        name = None
        if use_llm:
            try:
                if style == "systemone":
                    name = _systemone_pick(base, texts, cands)
                else:
                    name = _llm_name(base, model, texts)
            except Exception:  # noqa: BLE001 - 501/超时等 → 留空下次可补
                name = None
            if name in used_names:
                name = None  # 同批重名(疑似复读例子):只保留首个(最大簇)
        if not name:
            if use_llm:
                continue  # LLM 未产出:留空待补,显示为"未命名",不写关键词名冒充
            name = "·".join(cands[:3]) or "未命名"
        conn.execute(
            "UPDATE cluster SET name=?, updated_at=? "
            "WHERE set_name=? AND cluster_id=?",
            (name, _now(), set_name, r["cluster_id"]))
        named += 1
        used_names.add(name)
    conn.commit()
    used = set_name if named else "none"
    return named, used


def _extract_name(msg: dict) -> str | None:
    """从 chat 响应消息提取名字;兼容思考型模型,并对不守格式的小模型做清洗:
    取末行、剥前缀,超 12 字或含句读(说明输出了句子)即判无效;
    另拒绝"元词"名字(模型在谈论任务而非给出名字,如"只输出""推文内容")。"""
    META_WORDS = re.compile(
        "推文|输出|名字|回答|候选|以下|符合|需要|内容|标签|类目名|直接|这些|测试")
    # few-shot 例子相关词:小模型爱复读例子,硬编码拒收(例子在 _llm_name 里是常量)
    EXAMPLE_WORDS = re.compile("日本旅行|京都红叶|东京咖啡|大阪美食|本地推理优化")
    # 白名单:名字只许 中文/字母/数字/空格 与 · - + & /,其余(括号引号逗号冒号等)全拒
    NAME_OK = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff ·\-+&/]{2,12}$")

    def clean(raw: str) -> str | None:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if not lines:
            return None
        s = lines[-1]
        for pre in ("名字:", "名字:", "类目名:", "名称:", "答案:", "name:"):
            if s.lower().startswith(pre.lower()):
                s = s[len(pre):].strip()
        s = s.strip("\"“”「」 \n\t*#-—")
        if not NAME_OK.match(s):
            return None
        # 纯 ASCII 名要求 ≥4 字符且非纯数字(拦 "Sp" 之类小模型噪声)
        if not re.search(r"[\u4e00-\u9fff]", s) and (len(s) < 4 or s.isdigit()):
            return None
        if META_WORDS.search(s) or EXAMPLE_WORDS.search(s):
            return None
        return s

    name = clean(msg.get("content") or "")
    if name:
        return name
    # JSON 路径:名字可能在 content 或 reasoning 的 {"name": "..."} 里
    for raw in (msg.get("content") or "", msg.get("reasoning_content") or ""):
        m = re.search(r'\{\s*"name"\s*:\s*"([^"]{2,24})"', raw)
        if m:
            c = clean(m.group(1))
            if c:
                return c
    rc = msg.get("reasoning_content") or ""
    for ln in reversed(rc.splitlines()):  # 思考型模型:从思考尾部找,走同一套清洗
        c = clean(ln)
        if c:
            return c
    return None


def _llm_name(base: str, model: str, texts: list[str]) -> str | None:
    """传统 LLM 命名:OpenAI 兼容 /v1/chat/completions(llama-server 等)。
    JSON 约束 + few-shot 提升小/思考型模型通过率;长度与字符合法性仍由门禁把关。"""
    if not texts:
        return None
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": 768,  # 思考型模型需要预算走完 reasoning 再输出
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system",
             "content": '给一组同主题推文起简短中文类目名。只输出JSON:{"name":"类目名"}。'},
            {"role": "user",
             "content": "推文:\nllama.cpp MTP 解码加速\n本地显存优化\n输出JSON:"},
            {"role": "assistant", "content": '{"name":"本地推理优化"}'},
            {"role": "user",
             "content": "推文:\n京都红叶季攻略\n东京咖啡店打卡\n输出JSON:"},
            {"role": "assistant", "content": '{"name":"日本旅行"}'},
            {"role": "user",
             "content": "推文:\n" + "\n---\n".join(texts[:8]) + "\n输出JSON:"},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _extract_name(data["choices"][0]["message"])


def _systemone_pick(base: str, texts: list[str], candidates: list[str],
                    post=None) -> str | None:
    """SystemOne 决策式命名:POST /v1/systemone,让 decision 模型从候选词里挑最佳。
    需要原生 decision 模型(openjev/clef/granite-switch 等);普通文本模型会 501。"""
    if not texts or not candidates:
        return None
    post = post or _json_post
    data = post(f"{base.rstrip('/')}/systemone", {
        "state": "\n---\n".join(texts[:8]),
        "questions": {
            "name": {
                "type": "choice",
                "instructions": "哪个词最适合作为这组同主题推文的类目名?",
                "criteria": {c: None for c in candidates},
            },
        },
    })
    name = (data.get("answers", {}).get("name", {}) or {}).get("choice")
    return (name or None) if name in candidates else None


def _json_post(url: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cluster_list(conn: sqlite3.Connection) -> dict:
    """两套分区的簇清单:{"keyword": [...], "llm": [...]},按规模降序。"""
    out = {"keyword": [], "llm": []}
    for r in conn.execute(
            "SELECT set_name, cluster_id, name, size FROM cluster "
            "ORDER BY size DESC"):
        out.setdefault(r["set_name"], []).append({
            "cluster_id": r["cluster_id"],
            "name": r["name"] or f"未命名#{r['cluster_id']}",
            "raw_name": r["name"],
            "size": r["size"],
        })
    return out
