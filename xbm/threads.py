"""兴趣线索分析:把每个主题簇的时间活动剖面分解为
持续线索 / 中断线索 / 热点脉冲 / 新芽 —— 与"月度动态"(算法镜子)互补。

设计立场:推荐引擎制造周期性热点,仅按月统计会沦为算法附庸;
本模块识别"曾经关注、后被断供"的中断线索,支撑回溯与系统性深入。
"""

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from . import config

DORMANT_MONTHS = 4   # 沉寂判定:距上次新增 ≥ N 个月
EMERGE_MONTHS = 2    # 新芽:首次出现距今 < N 个月
KW_MIN_SIZE = 4      # 关键词微线索最小规模(千级碎簇过滤)


def _diff_months(a: str, b: str) -> int:
    """b - a 的月数(a/b 形如 YYYY-MM)。"""
    if not a or not b:
        return 0
    ya, ma, yb, mb = int(a[:4]), int(a[5:7]), int(b[:4]), int(b[5:7])
    return (yb - ya) * 12 + (mb - ma)


def _sparkline(months: dict, now_m: str, width: int = 12) -> str:
    """近 width 个月的活跃条形(文本 sparkline)。"""
    blocks = "▁▂▃▅▆"
    y, m = int(now_m[:4]), int(now_m[5:7])
    out = []
    for i in range(width - 1, -1, -1):
        mm = (m - 1 - i) % 12 + 1
        yy = y + (m - 1 - i) // 12
        c = months.get(f"{yy}-{mm:02d}", 0)
        out.append(blocks[min(c, 4)] if c else "·")
    return "".join(out)


def analyze(conn: sqlite3.Connection, now: str | None = None) -> dict:
    """计算两套分区(语义/字面)全部主题簇的时间剖面并分类。
    now: 覆盖"当前月"(YYYY-MM,测试用);默认 UTC 当前月。"""
    now_m = now or datetime.now(timezone.utc).strftime("%Y-%m")
    names = {"llm": {}, "keyword": {}}
    for set_name, col in (("llm", "cluster_llm"), ("keyword", "cluster_keyword")):
        names[set_name] = {c: n for c, n in conn.execute(
            "SELECT cluster_id, COALESCE(name, '未命名#' || cluster_id) "
            f"FROM cluster WHERE set_name='{set_name}'")}

    buckets: dict[tuple, dict] = defaultdict(lambda: {"months": defaultdict(int), "n": 0})
    rows = conn.execute(
        "SELECT cluster_llm, cluster_keyword, "
        "       COALESCE((SELECT bookmarked_at FROM bookmark b WHERE b.tweet_id=t.tweet_id), "
        "                t.created_at) AS ds "
        "FROM tweet t").fetchall()
    for r in rows:
        m = (r["ds"] or "")[:7]
        if not m:
            continue
        for set_name, col in (("llm", "cluster_llm"), ("keyword", "cluster_keyword")):
            cid = r[col]
            if cid is not None:
                b = buckets[(set_name, cid)]
                b["months"][m] += 1
                b["n"] += 1

    threads = []
    for (set_name, cid), b in buckets.items():
        if set_name == "keyword" and b["n"] < KW_MIN_SIZE:
            continue
        ms = sorted(b["months"])
        first, last = ms[0], ms[-1]
        active_months = len(ms)
        span = _diff_months(first, last)
        dormancy = _diff_months(last, now_m)
        c3 = sum(b["months"].get(
            f"{int(now_m[:4]) - (1 if int(now_m[5:7]) - i <= 0 else 0)}"
            f"-{(int(now_m[5:7]) - i - 1) % 12 + 1:02d}", 0) for i in range(3))
        t = {
            "set": set_name, "cluster_id": cid,
            "name": names[set_name].get(cid, f"未命名#{cid}"),
            "size": b["n"], "first": first, "last": last,
            "active_months": active_months, "span": span,
            "dormancy": dormancy, "recent3": c3,
            "emerging": _diff_months(first, now_m) < EMERGE_MONTHS,
            "spark": _sparkline(b["months"], now_m),
            "months": dict(b["months"]),
        }
        t["class"] = _classify(t)
        threads.append(t)

    result = {"persistent": [], "dormant": [], "pulse": [], "emerging": []}
    for t in threads:
        result[t["class"]].append(t)
    result["persistent"].sort(key=lambda t: -t["size"])
    result["dormant"].sort(key=lambda t: -t["size"])
    result["pulse"].sort(key=lambda t: -t["size"])
    result["emerging"].sort(key=lambda t: -t["size"])
    return {"threads": result, "now": now_m, "generated_at": datetime.now(
        timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}


def _classify(t: dict) -> str:
    if t["emerging"]:
        return "emerging"
    if t["active_months"] <= 2 and t["span"] <= 1:
        return "pulse"
    if t["dormancy"] >= DORMANT_MONTHS:
        return "dormant"
    if t["active_months"] >= 4 or t["span"] >= 5:
        return "persistent"
    return "persistent"  # 其余温和关注并入持续视图


# ---------------- LLM 线索卡片(可选增强) ----------------

def thread_prompt(t: dict, samples: list[str]) -> str:
    period = f"{t['first']} ~ {t['last']}"
    return (f"线索「{t['name']}」:共 {t['size']} 条书签,活跃 {t['active_months']} 个月"
            f"({period})"
            + (f",已沉寂 {t['dormancy']} 个月" if t["class"] == "dormant" else "")
            + "\n代表内容:\n" + "\n".join(f"- {s}" for s in samples[:6]))


def llm_cards(base: str, model: str, card_inputs: list[dict]) -> dict:
    """card_inputs: [{key, prompt}];返回 {key: {about, next}}。每条一次调用,
    JSON 约束 + few-shot;思考型模型 token 预算 2048→3072 重试;单条失败跳过。"""
    out = {}
    for ci in card_inputs:
        result = None
        for max_tokens in (2048, 3072):
            body = json.dumps({
                "model": model, "temperature": 0.3, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system",
                     "content": '你在为用户的知识线索写卡片。只输出JSON:'
                                '{"about":"2-3句:这条线索收集的是什么、围绕什么问题",'
                                '"next":"2-3句:下一步深入的具体建议,基于已收集内容指出缺口"}'},
                    {"role": "user",
                     "content": "线索「本地大模型推理加速」:共 120 条,活跃 10 个月\n"
                                "代表内容:\n- llama.cpp MTP 解码加速\n- 显存优化技巧\n"
                                "输出JSON:"},
                    {"role": "assistant",
                     "content": '{"about":"围绕在消费级硬件上跑大模型的推理加速技术,覆盖解码优化与显存管理。","next":"已收集偏技巧片段,建议系统梳理 llama.cpp 的量化方案对比,并实测一条 MTP+KV 缓存组合在本机的收益。"}'},
                    {"role": "user", "content": ci["prompt"] + "\n输出JSON:"},
                ]}, ensure_ascii=False).encode("utf-8")
            try:
                raw = urllib_request(f"{base.rstrip('/')}/chat/completions", body,
                                     timeout=240)
                msg = json.loads(raw)["choices"][0]["message"]
                text = (msg.get("content") or "").strip()
                if not text:  # 思考型模型:正文空,从 reasoning 里捞 JSON
                    rc = msg.get("reasoning_content") or ""
                    m = re.search(r'\{[\s\S]*?"about"[\s\S]*?"next"[\s\S]*?\}', rc)
                    text = m.group(0) if m else ""
                if text:
                    d = json.loads(text)
                    about, nxt = (d.get("about") or "").strip(), (d.get("next") or "").strip()
                    if about or nxt:
                        result = {"about": about[:400], "next": nxt[:400]}
                        break
            except Exception:  # noqa: BLE001 - 换更大预算重试
                continue
        if result:
            out[ci["key"]] = result
    return out


def urllib_request(url: str, body: bytes, timeout: int = 120) -> str:
    import urllib.request
    r = urllib.request.Request(url, data=body,
                               headers={"Content-Type": "application/json"},
                               method="POST")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def thread_samples(conn: sqlite3.Connection, set_name: str, cluster_id: int,
                   limit: int = 6) -> list[str]:
    """簇代表内容(复用聚类时存的质心近邻样本)。"""
    col = f"cluster_{set_name}"
    row = conn.execute(
        "SELECT sample_json FROM cluster WHERE set_name=? AND cluster_id=?",
        (set_name, cluster_id)).fetchone()
    ids = json.loads(row[0]) if row and row[0] else []
    out = []
    for tid in ids[:limit]:
        r = conn.execute("SELECT text FROM tweet WHERE tweet_id=?", (tid,)).fetchone()
        if r and r["text"]:
            out.append(r["text"][:80].replace("\n", " "))
    if not out:  # 老分区无样本兜底:取最新几条
        for (t,) in conn.execute(
                f"SELECT text FROM tweet WHERE {col}=? AND text != '' "
                "ORDER BY created_at DESC LIMIT ?", (cluster_id, limit)):
            out.append(t[:80].replace("\n", " "))
    return out


def save_cards(conn: sqlite3.Connection, cards: dict) -> None:
    import datetime as _dt
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('threads_cards', ?)",
                 (json.dumps({"updated_at": _dt.datetime.now(timezone.utc)
                              .strftime("%Y-%m-%d %H:%M:%S"), "cards": cards},
                             ensure_ascii=False),))
    conn.commit()


def load_cards(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='threads_cards'").fetchone()
    return json.loads(row[0]) if row else {"cards": {}}
