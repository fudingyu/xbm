"""数据质量报告:总量/构成/语言/作者/域名/嵌入覆盖 —— 决定后续增强项的取舍。"""

import json
import sqlite3
from collections import Counter

from . import embed, textproc


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "-"


def build_report(conn: sqlite3.Connection) -> str:
    out = []
    total = conn.execute("SELECT COUNT(*) FROM tweet").fetchone()[0]
    bookmarks = conn.execute("SELECT COUNT(*) FROM bookmark").fetchone()[0]
    out.append(f"=== X 书签数据报告 ===")
    out.append(f"推文总数(去重后): {total}   书签记录: {bookmarks}")
    if not total:
        return "\n".join(out) + "\n(库为空,先运行 sync 或 import)"

    row = conn.execute(
        "SELECT MIN(date_sort), MAX(date_sort) FROM ("
        "  SELECT COALESCE((SELECT bookmarked_at FROM bookmark b WHERE b.tweet_id=t.tweet_id), "
        "                  t.created_at) AS date_sort FROM tweet t)"
    ).fetchone()
    out.append(f"时间范围: {row[0]} ~ {row[1]}")

    media = conn.execute("SELECT COUNT(*) FROM tweet WHERE json_array_length(media_json)>0").fetchone()[0]
    urls = conn.execute("SELECT COUNT(*) FROM tweet WHERE json_array_length(urls_json)>0").fetchone()[0]
    rt = conn.execute("SELECT COUNT(*) FROM tweet WHERE is_retweet=1").fetchone()[0]
    rp = conn.execute("SELECT COUNT(*) FROM tweet WHERE is_reply=1").fetchone()[0]
    out.append(f"含媒体: {media} ({_pct(media, total)})   含外链: {urls} ({_pct(urls, total)})")
    out.append(f"转推: {rt} ({_pct(rt, total)})   回复: {rp} ({_pct(rp, total)})")

    lang_stat = Counter()
    for (text,) in conn.execute("SELECT text FROM tweet"):
        lang_stat[textproc.detect_lang(text)] += 1
    out.append("语言(启发式): " + "  ".join(
        f"{k}: {v} ({_pct(v, total)})" for k, v in lang_stat.most_common()))

    lengths = [r[0] for r in conn.execute("SELECT LENGTH(text) FROM tweet")]
    buckets = [0, 50, 100, 200, 400, 10**9]
    bucket_n = [0] * (len(buckets) - 1)
    for L in lengths:
        for i in range(len(buckets) - 1):
            if buckets[i] <= L < buckets[i + 1]:
                bucket_n[i] += 1
                break
    out.append("文本长度分布: " + "  ".join(
        f"{buckets[i]}-{buckets[i+1] if buckets[i+1] < 10**9 else '+'}: {bucket_n[i]}"
        for i in range(len(buckets) - 1)))

    out.append("Top 作者:")
    for r in conn.execute(
            "SELECT COALESCE(author_handle,'(未知)') AS h, COUNT(*) AS n "
            "FROM tweet GROUP BY h ORDER BY n DESC LIMIT 10"):
        out.append(f"  @{r['h']}: {r['n']}")

    domains = Counter()
    for (uj,) in conn.execute("SELECT urls_json FROM tweet WHERE urls_json != '[]'"):
        for u in json.loads(uj):
            d = u.split("//", 1)[-1].split("/", 1)[0]
            domains[d.removeprefix("www.")] += 1
    if domains:
        out.append("Top 外链域名:")
        for d, n in domains.most_common(10):
            out.append(f"  {d}: {n}")

    done, tot = embed.coverage(conn)
    out.append(f"嵌入覆盖: {done}/{tot} ({_pct(done, tot)})")
    return "\n".join(out)
