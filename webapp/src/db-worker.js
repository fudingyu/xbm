// db-worker.js — SQLite WASM(opfs-sahpool VFS)专属 worker。
// sahpool 用同步文件句柄(仅 worker 可用),持久化在浏览器 OPFS;同一时刻只允许一个连接。
import sqlite3InitModule from '@sqlite.org/sqlite-wasm';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS tweet(
  tweet_id      TEXT PRIMARY KEY,
  text          TEXT NOT NULL DEFAULT '',
  author_handle TEXT, author_name TEXT, created_at TEXT,
  media_json    TEXT NOT NULL DEFAULT '[]',
  urls_json     TEXT NOT NULL DEFAULT '[]',
  is_retweet    INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0,
  embed_text    TEXT, embedding BLOB, embedding_model TEXT,
  embedded_at   TEXT, imported_at   TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
`;

let db = null;
let initError = null;
const logs = [];
const initPromise = (async () => {
  const sqlite3 = await sqlite3InitModule({
    print: (...a) => logs.push('[log] ' + a.join(' ')),
    printErr: (...a) => logs.push('[err] ' + a.join(' ')),
  });
  try {
    db = new sqlite3.oo1.DB('/xbm.sqlite3', 'ct', 'opfs-sahpool');
  } catch (err) {
    initError = '本地数据库打开失败(可能被另一个标签页占用,请只开一个 xbm 页面): ' + err
      + ' | init logs: ' + logs.join(' || ');
    throw new Error(initError);
  }
  db.exec(SCHEMA);
  return true;
})();

const q = (sql, bind = []) => db.selectObjects(sql, bind);
const run = (sql, bind = []) => db.exec({ sql, bind });

const HANDLERS = {
  async vfsInfo() {
    const find = (name) => {
      try { return sqlite3.capi.sqlite3_vfs_find(name) !== 0 ? 'yes' : 'no'; }
      catch (e) { return 'err:' + e; }
    };
    return {
      logs: logs.slice(-12),
      opfs: find('opfs'), sahpool: find('opfs-sahpool'), default: find(null),
    };
  },
  async info() {
    return {
      tweets: q('SELECT COUNT(*) AS n FROM tweet')[0].n,
      embedded: q('SELECT COUNT(*) AS n FROM tweet WHERE LENGTH(embedding) > 0')[0].n,
    };
  },
  async merge({ rows }) {
    let added = 0, dup = 0;
    for (const r of rows) {
      if (!r.tweet_id || !r.text) continue;
      const exists = q('SELECT 1 AS x FROM tweet WHERE tweet_id=?', [r.tweet_id]);
      if (exists.length) { dup++; continue; }
      run(
        `INSERT INTO tweet(tweet_id, text, author_handle, author_name, created_at,
           media_json, urls_json, is_retweet, is_reply, embed_text, imported_at)
         VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))`,
        [r.tweet_id, r.text, r.author_handle || null, r.author_name || null,
         r.created_at || null, JSON.stringify(r.media || []),
         JSON.stringify(r.urls || []), r.is_retweet ? 1 : 0, r.is_reply ? 1 : 0,
         r.embed_text || null]);
      added++;
    }
    return { added, dup };
  },
  async pending({ limit }) {
    return q('SELECT tweet_id, embed_text FROM tweet WHERE embedding IS NULL AND embed_text IS NOT NULL LIMIT ?', [limit]);
  },
  async storeVecs({ vectors }) {
    for (const v of vectors) {
      run('UPDATE tweet SET embedding=?, embedding_model=?, embedded_at=datetime(\'now\') WHERE tweet_id=? AND embedding IS NULL',
        [v.vec, v.model, v.id]);
    }
    return vectors.length;
  },
  async allVecs() {
    return q(`SELECT tweet_id, text, author_handle, author_name, created_at,
                      media_json, urls_json, embedding FROM tweet WHERE LENGTH(embedding) > 0`);
  },
  async allRows({ limit = 200 }) {
    return q(`SELECT tweet_id, text, author_handle, author_name, created_at,
                      media_json, urls_json FROM tweet ORDER BY created_at DESC LIMIT ?`, [limit]);
  },
  async deleteTweet({ tweet_id }) {
    run('DELETE FROM tweet WHERE tweet_id=?', [tweet_id]);
    return true;
  },
  async setMeta({ key, value }) {
    run('INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)', [key, value]);
    return true;
  },
  async getMeta({ key }) {
    const r = q('SELECT value FROM meta WHERE key=?', [key]);
    return r.length ? r[0].value : null;
  },
  async dump() {
    return q('SELECT * FROM tweet');
  },
  async wipe() {
    run('DELETE FROM tweet; DELETE FROM meta');
    return true;
  },
};

self.onmessage = async (e) => {
  const { id, op, payload } = e.data || {};
  try {
    await initPromise;
    const handler = HANDLERS[op];
    if (!handler) throw new Error('unknown op: ' + op);
    const result = await handler(payload || {});
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: String((err && err.message) || err) });
  }
};
