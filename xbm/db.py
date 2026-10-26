"""SQLite 库:书签/推文表 + FTS5(trigram,支持中文子串)。"""

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tweet(
  tweet_id        TEXT PRIMARY KEY,
  text            TEXT NOT NULL DEFAULT '',
  author_handle   TEXT,
  author_name     TEXT,
  created_at      TEXT,
  lang            TEXT,
  media_json      TEXT NOT NULL DEFAULT '[]',
  urls_json       TEXT NOT NULL DEFAULT '[]',
  is_retweet      INTEGER NOT NULL DEFAULT 0,
  is_reply        INTEGER NOT NULL DEFAULT 0,
  category        TEXT,
  category_score  REAL,
  cluster_keyword INTEGER,
  cluster_llm     INTEGER,
  map_x           REAL,
  map_y           REAL,
  cluster_id      INTEGER,
  source          TEXT,
  raw_json        TEXT,
  embed_text      TEXT,
  embedding       BLOB,
  embedding_model TEXT,
  embedded_at     TEXT,
  imported_at     TEXT
);

CREATE TABLE IF NOT EXISTS bookmark(
  tweet_id      TEXT NOT NULL,
  bookmarked_at TEXT,
  first_seen    TEXT,
  source        TEXT NOT NULL DEFAULT 'archive',
  PRIMARY KEY(tweet_id, source)
);

CREATE TABLE IF NOT EXISTS cluster(
  set_name    TEXT NOT NULL,       -- "keyword" | "llm":两套独立聚类分区
  cluster_id  INTEGER NOT NULL,
  name        TEXT,
  size        INTEGER,
  sample_json TEXT,
  centroid_json TEXT,              -- 归一化簇心,增量归位用(缺省自愈回填)
  updated_at  TEXT,
  PRIMARY KEY(set_name, cluster_id)
);

CREATE INDEX IF NOT EXISTS idx_tweet_created ON tweet(created_at);
CREATE INDEX IF NOT EXISTS idx_tweet_handle  ON tweet(author_handle);
CREATE INDEX IF NOT EXISTS idx_tweet_category ON tweet(category);
CREATE INDEX IF NOT EXISTS idx_tweet_ckw ON tweet(cluster_keyword);
CREATE INDEX IF NOT EXISTS idx_tweet_cllm ON tweet(cluster_llm);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS tweet_fts USING fts5(
  text, author, content='tweet', content_rowid='rowid', tokenize='trigram'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS tweet_ai AFTER INSERT ON tweet BEGIN
  INSERT INTO tweet_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author_handle);
END;
CREATE TRIGGER IF NOT EXISTS tweet_ad AFTER DELETE ON tweet BEGIN
  INSERT INTO tweet_fts(tweet_fts, rowid, text, author)
  VALUES('delete', old.rowid, old.text, old.author_handle);
END;
CREATE TRIGGER IF NOT EXISTS tweet_au AFTER UPDATE ON tweet BEGIN
  INSERT INTO tweet_fts(tweet_fts, rowid, text, author)
  VALUES('delete', old.rowid, old.text, old.author_handle);
  INSERT INTO tweet_fts(rowid, text, author) VALUES (new.rowid, new.text, new.author_handle);
END;
"""


def _migrate(conn: sqlite3.Connection):
    """给已有库补 M3 列(category/cluster);全新库(无 tweet 表)跳过,由 SCHEMA 全量创建。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tweet)")}
    if not cols:
        return
    for col, ddl in (
        ("category", "ALTER TABLE tweet ADD COLUMN category TEXT"),
        ("category_score", "ALTER TABLE tweet ADD COLUMN category_score REAL"),
        ("cluster_id", "ALTER TABLE tweet ADD COLUMN cluster_id INTEGER"),
    ):
        if col not in cols:
            conn.execute(ddl)
    # M3.2:三维度分类模型——tweet 增 keyword/llm 两套簇列;cluster 表升级为分区分表。
    # 旧单分区(含 name_keyword/name_llm 过渡态)整体迁为 keyword 分区,不丢数据。
    for col in ("cluster_keyword", "cluster_llm", "map_x", "map_y"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE tweet ADD COLUMN {col} "
                f"{'REAL' if col.startswith('map_') else 'INTEGER'}")
    # 簇心列:已有分区库补列(老分区无簇心,首次归位时自愈回填)
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(cluster)")}
    if ccols and "centroid_json" not in ccols:
        conn.execute("ALTER TABLE cluster ADD COLUMN centroid_json TEXT")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(cluster)")}
    if ccols and "set_name" not in ccols:  # 整块仅首次执行
        conn.execute("ALTER TABLE cluster RENAME TO cluster_legacy")
        conn.executescript(
            "CREATE TABLE cluster("
            " set_name TEXT NOT NULL, cluster_id INTEGER NOT NULL, name TEXT,"
            " size INTEGER, sample_json TEXT, centroid_json TEXT, updated_at TEXT,"
            " PRIMARY KEY(set_name, cluster_id))")
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(cluster_legacy)")}
        name_expr = "COALESCE(name, name_keyword, name_llm)" if "name_keyword" in lcols else "name"
        conn.execute(
            "INSERT INTO cluster(set_name, cluster_id, name, size, sample_json, updated_at) "
            f"SELECT 'keyword', cluster_id, {name_expr}, size, sample_json, updated_at "
            "FROM cluster_legacy")
        conn.execute("DROP TABLE cluster_legacy")
        if "cluster_id" in cols:  # 旧单分区标签 → keyword 分区列(一次性)
            conn.execute(
                "UPDATE tweet SET cluster_keyword=cluster_id "
                "WHERE cluster_keyword IS NULL AND cluster_id IS NOT NULL")
    conn.commit()


def connect(db_file: Path | str | None = None) -> sqlite3.Connection:
    """打开(必要时创建)数据库,启用 WAL 与外键,确保 schema 就绪。"""
    path = Path(db_file) if db_file else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")  # 并发写入时等待而非立刻 locked
    _migrate(conn)  # 先补列,SCHEMA 里的新列索引才不会在老库上报错
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA + FTS_TRIGGERS)
    except sqlite3.OperationalError:
        # 旧版 SQLite 无 trigram,退回默认分词(中文关键词检索退化,语义检索不受影响)
        conn.executescript(
            FTS_SCHEMA.replace(", tokenize='trigram'", "")
            + FTS_TRIGGERS
        )
    return conn
