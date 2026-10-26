"""关注焦点时间脉络:按月/季聚合书签主题摘要,LLM 撰写演变叙事。

摘要维度:类目分布 + LLM 语义簇 + 关键词字面簇 + 代表推文。
结果缓存于 meta(timeline_<unit>),重新生成覆盖。
"""

import json
import sqlite3
import urllib.request
from collections import Counter
from datetime import datetime, timezone

MONTH_TOP = 3  # 每期各类主题取前 N


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_digest(conn: sqlite3.Connection, unit: str = "month") -> list[dict]:
    """按月(unit=month)或季(unit=quarter)聚合主题摘要。"""
    def period(d: str) -> str:
        if not d:
            return "未知"
        y, m = int(d[:4]), int(d[5:7])
        if unit == "quarter":
            return f"{y}Q{(m - 1) // 3 + 1}"
        return f"{y}-{m:02d}"

    rows = conn.execute(
        "SELECT t.tweet_id, t.text, t.category, t.cluster_keyword, t.cluster_llm, "
        "       COALESCE((SELECT bookmarked_at FROM bookmark b WHERE b.tweet_id=t.tweet_id), "
        "                t.created_at) AS ds "
        "FROM tweet t WHERE t.text != '' ORDER BY ds").fetchall()
    kw_names = {c: n for c, n in conn.execute(
        "SELECT cluster_id, COALESCE(name,'?') FROM cluster WHERE set_name='keyword'")}
    llm_names = {c: n for c, n in conn.execute(
        "SELECT cluster_id, COALESCE(name,'?') FROM cluster WHERE set_name='llm'")}

    buckets: dict[str, dict] = {}
    for r in rows:
        p = period(r["ds"])
        b = buckets.setdefault(p, {
            "period": p, "total": 0, "cats": Counter(),
            "llm": Counter(), "kw": Counter(), "authors": Counter(), "samples": []})
        b["total"] += 1
        if r["category"]:
            b["cats"][r["category"]] += 1
        if r["cluster_llm"] is not None:
            b["llm"][llm_names.get(r["cluster_llm"], "?")] += 1
        if r["cluster_keyword"] is not None:
            b["kw"][kw_names.get(r["cluster_keyword"], "?")] += 1
        if len(b["samples"]) < 2:  # 代表作:每期取前两条(时间序)
            b["samples"].append((r["tweet_id"], (r["text"] or "")[:48]))

    out = []
    for p in sorted(buckets):
        b = buckets[p]
        out.append({
            "period": p,
            "total": b["total"],
            "cats": [[k, v] for k, v in b["cats"].most_common(MONTH_TOP)],
            "llm": [[k, v] for k, v in b["llm"].most_common(MONTH_TOP)],
            "kw": [[k, v] for k, v in b["kw"].most_common(MONTH_TOP)],
            "samples": [t for _, t in b["samples"]],
        })
    return out


def render_digest(digest: list[dict], compact: bool = False) -> str:
    """摘要渲染成文本。compact=True 供 LLM(每期一行、仅主题,防长摘要耗尽
    思考型模型 token);默认版含代表作,供 UI 展示。"""
    lines = []
    for d in digest:
        if compact:
            parts = [f"{d['period']}({d['total']})"]
            if d["llm"]:
                parts.append("/".join(f"{k}{v}" for k, v in d["llm"][:2]))
            elif d["cats"]:
                parts.append("/".join(f"{k}{v}" for k, v in d["cats"][:2]))
            lines.append(" ".join(parts))
            continue
        parts = [f"{d['period']}({d['total']}条)"]
        if d["cats"]:
            parts.append("类目:" + ",".join(f"{k}{v}" for k, v in d["cats"]))
        if d["llm"]:
            parts.append("主题:" + ",".join(f"{k}{v}" for k, v in d["llm"]))
        if d["kw"]:
            parts.append("关键词:" + ",".join(f"{k}{v}" for k, v in d["kw"]))
        lines.append(" | ".join(parts))
        for s in d["samples"]:
            lines.append(f"  例:「{s}」")
    return "\n".join(lines)


def llm_narrative(base: str, model: str, digest_text: str) -> str:
    """让 LLM 依据摘要撰写关注焦点演变脉络;思考型模型正文为空时加倍重试一次。"""
    last_err = None
    for max_tokens in (2048, 4096):
        body = json.dumps({
            "model": model, "temperature": 0.4, "max_tokens": max_tokens,
            "messages": [
                {"role": "system",
                 "content": "你根据用户每月的书签主题统计,梳理他关注焦点随时间演变的脉络。"
                            "用中文按时间顺序写:每期一两句(指出兴起/消退/转移的主题),"
                            "结尾用一段总结整体演变主线。直接输出正文,不要开头客套。"},
                {"role": "user", "content": "书签月度统计:\n" + digest_text},
            ],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text[:6000]
            last_err = "模型未返回正文"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise ValueError(f"模型未返回正文({last_err}),可换更大模型或减少时间范围")


def llm_narrative_chunked(base: str, model: str, digest_text: str,
                          chunk_lines: int = 14) -> str:
    """长摘要分段生成:每段用与整版完全相同的提示词模板(实测可出文),
    走 llm_narrative 的重试梯子;段间拼接。避免"继续接续"类变体提示词
    (思考型小模型会卡死输出空正文)。"""
    lines = digest_text.splitlines()
    if len(lines) <= chunk_lines + 2:
        return llm_narrative(base, model, digest_text)
    parts = []
    for i in range(0, len(lines), chunk_lines):
        seg = "\n".join(lines[i : i + chunk_lines])
        try:
            parts.append(llm_narrative(base, model, seg))
        except Exception:  # noqa: BLE001 - 单段失败跳过,不让整体报废
            continue
    if not parts:
        raise ValueError("模型未返回正文")
    return "\n".join(parts)[:8000]


def filter_digest(digest: list[dict], window: str) -> list[dict]:
    """按时间窗过滤期(month 期名 YYYY-MM 字典序可比;quarter 期名单独换算)。"""
    cutoff = window_cutoff(window)
    if not cutoff:
        return digest
    cut_y, cut_m = int(cutoff[:4]), int(cutoff[5:7])
    out = []
    for d in digest:
        p = d["period"]
        if p == "未知":
            continue
        if "Q" in p:
            ok = (int(p[:4]), int(p[5])) >= (cut_y, (cut_m - 1) // 3 + 1)
        else:
            ok = p >= cutoff
        if ok:
            out.append(d)
    return out


def save_timeline(conn: sqlite3.Connection, unit: str, digest: list[dict],
                  narrative: str | None, window: str = "all") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (f"timeline_{unit}_{window}", json.dumps({
            "generated_at": _now(), "unit": unit, "window": window,
            "digest": digest, "narrative": narrative,
        }, ensure_ascii=False)))
    conn.commit()


def load_timeline(conn: sqlite3.Connection, unit: str, window: str = "all") -> dict | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (f"timeline_{unit}_{window}",)).fetchone()
    return json.loads(row[0]) if row else None


def window_cutoff(window: str) -> str | None:
    """时间窗('all'|'12'|'24')→ 期字符串下限(YYYY-MM;季度口径按月换算)。"""
    if window in ("12", "24"):
        now = datetime.now()
        months = int(window)
        y = now.year - (now.month - 1 + months) // 12
        m = (now.month - 1 + months) % 12 + 1
        return f"{y}-{m:02d}"
    return None
