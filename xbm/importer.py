"""统一入库:存档/GraphQL/API 各数据源共用,按 tweet_id 去重。"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import textproc


@dataclass
class ImportStats:
    entries: int = 0
    tweets_new: int = 0
    tweets_dup: int = 0
    bookmarks_new: int = 0
    no_content: int = 0
    joined_from_tweets: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def import_rows(conn: sqlite3.Connection, rows, source: str) -> ImportStats:
    """把 normalize 好的行写入 tweet/bookmark 表。已有 tweet_id 不覆盖(首见为准)。"""
    stats = ImportStats()
    now = _now()
    for row in rows:
        stats.entries += 1
        tid = row.get("tweet_id")
        if not tid or not row.get("text"):
            stats.no_content += 1
            continue
        embed_text = textproc.build_embed_text(row.get("author_handle"), row["text"], row.get("quoted_text"))
        lang = row.get("lang") or textproc.detect_lang(row["text"])
        cur = conn.execute(
            """INSERT OR IGNORE INTO tweet(
                 tweet_id, text, author_handle, author_name, created_at, lang,
                 media_json, urls_json, is_retweet, is_reply, source, raw_json,
                 embed_text, imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tid, row["text"], row.get("author_handle"), row.get("author_name"),
                row.get("created_at"), lang,
                json.dumps(row.get("media", []), ensure_ascii=False),
                json.dumps(row.get("urls", []), ensure_ascii=False),
                int(bool(row.get("is_retweet"))), int(bool(row.get("is_reply"))),
                source, json.dumps(row.get("raw", {}), ensure_ascii=False)[:20000],
                embed_text, now,
            ),
        )
        if cur.rowcount == 0:
            stats.tweets_dup += 1
        else:
            stats.tweets_new += 1

        bookmarked_at = row.get("bookmarked_at")
        old = conn.execute(
            "SELECT bookmarked_at FROM bookmark WHERE tweet_id=? AND source=?",
            (tid, source),
        ).fetchone()
        if old is None:
            conn.execute(
                "INSERT INTO bookmark(tweet_id, bookmarked_at, first_seen, source) VALUES(?,?,?,?)",
                (tid, bookmarked_at, now, source),
            )
            stats.bookmarks_new += 1
        elif bookmarked_at and (not old["bookmarked_at"] or bookmarked_at < old["bookmarked_at"]):
            conn.execute(
                "UPDATE bookmark SET bookmarked_at=? WHERE tweet_id=? AND source=?",
                (bookmarked_at, tid, source),
            )
    conn.commit()
    return stats


def import_archive(conn: sqlite3.Connection, archive_path, source: str = "archive") -> ImportStats:
    from . import archive  # 延迟导入,避免循环依赖

    result = archive.load_archive(archive_path)
    stats = import_rows(conn, result.rows, source)
    stats.no_content = result.no_content
    stats.joined_from_tweets = result.joined_from_tweets
    return stats
