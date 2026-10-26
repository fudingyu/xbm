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
let dbOpenError = null;
let sqlite3 = null;
// 存储模式:'opfs'(浏览器私有存储,默认) | 'mem-file'(用户选定的真实 .db 文件,
// 内存中运行 + 显式写回句柄)。文件句柄由主线程通过 setFileHandle 传入。
let mode = 'opfs';
let fileHandle = null;
let dirty = false;
const logs = [];
// sqlite3.config.warn/error 直通 console,先劫持以捕获被吞掉的安装失败原因
for (const m of ["warn", "error", "log"]) {
  const orig = console[m].bind(console);
  console[m] = (...a) => { logs.push("[" + m + "] " + a.map(String).join(" ").slice(0, 300)); orig(...a); };
}
const modPromise = (async () => {
  sqlite3 = await sqlite3InitModule({
    print: (...a) => logs.push('[log] ' + a.join(' ')),
    printErr: (...a) => logs.push('[err] ' + a.join(' ')),
  });
  // opfs-sahpool 需显式安装(不调用则 VFS 不存在 —— 之前"静默失败"的根因)
  let vfsName = 'opfs-sahpool';
  try {
    const pool = await sqlite3.installOpfsSAHPoolVfs({ verbosity: 1 });
    vfsName = pool.vfsName || vfsName;
    logs.push('[sahpool] installed as ' + vfsName);
  } catch (err) {
    logs.push('[sahpool] install failed: ' + err);
  }
  try {
    db = new sqlite3.oo1.DB('/xbm.sqlite3', 'ct', vfsName);
    db.exec(SCHEMA);
  } catch (err) {
    dbOpenError = String((err && err.message) || err) + ' | init logs: ' + logs.join(' || ');
  }
  return sqlite3;
})();
const ensureDb = async () => {
  await modPromise;
  if (!db) throw new Error('本地数据库不可用: ' + dbOpenError);
};

const q = (sql, bind = []) => db.selectObjects(sql, bind);
const run = (sql, bind = []) => { db.exec({ sql, bind }); if (mode === 'mem-file') dirty = true; };

// ---- 存储模式切换(双模式:OPFS 浏览器私有 / 用户选定的真实 .db 文件) ----
async function closeCurrent() {
  if (!db) return;
  try { db.close(); } catch (e) { logs.push('[close] ' + e); }
  db = null;
}

// WAL 模式镜像直接 deserialize 会让 SQLite 去找 -wal 文件(CANTOPEN)。
// 标准处理:把头部 write/read version 字节从 2(WAL)改为 1(legacy)。
function deWal(bytes) {
  if (bytes.length > 20 && bytes[18] === 2 && bytes[19] === 2) {
    const copy = bytes.slice();
    copy[18] = 1; copy[19] = 1;
    return copy;
  }
  return bytes;
}

async function useFileDb({ bytes, name, migrate }) {
  await modPromise;
  // 先读旧库全部行(含向量),切换后原样迁入
  const carry = migrate && db ? q('SELECT * FROM tweet') : null;
  closeCurrent();
  mode = 'mem-file';
  dirty = carry && carry.length ? true : false;
  db = new sqlite3.oo1.DB(':memory:', 'ct');
  if (bytes && bytes.byteLength) {
    const img = deWal(bytes);
    const p = sqlite3.wasm.allocFromTypedArray(img);
    try {
      // flags: 1=FREEONCLOSE 4=RESIZEABLE(内存库要可增长)
      const rc = sqlite3.capi.sqlite3_deserialize(db.pointer, 'main', p,
        img.byteLength, img.byteLength, 1 | 4);
      if (rc !== 0) throw new Error('deserialize rc=' + rc);
    } catch (e) {
      sqlite3.wasm.dealloc(p);
      throw e;
    }
  }
  db.exec(SCHEMA); // 全新空文件时建表
  if (carry && carry.length) {
    for (const r of carry) {
      run(`INSERT OR IGNORE INTO tweet(tweet_id, text, author_handle, author_name, created_at,
         media_json, urls_json, is_retweet, is_reply, embed_text, embedding,
         embedding_model, embedded_at, imported_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
        [r.tweet_id, r.text, r.author_handle || null, r.author_name || null,
         r.created_at || null, r.media_json || '[]', r.urls_json || '[]',
         r.is_retweet ? 1 : 0, r.is_reply ? 1 : 0, r.embed_text || null,
         r.embedding || null, r.embedding_model || null, r.embedded_at || null,
         r.imported_at || null]);
    }
  }
  return { mode, name, tweets: q('SELECT COUNT(*) AS n FROM tweet')[0].n };
}

async function useOpfsDb() {
  await modPromise;
  closeCurrent();
  mode = 'opfs';
  fileHandle = null;
  dirty = false;
  db = new sqlite3.oo1.DB('/xbm.sqlite3', 'ct', 'opfs-sahpool');
  db.exec(SCHEMA);
  return { mode, tweets: q('SELECT COUNT(*) AS n FROM tweet')[0].n };
}

async function saveFile() {
  if (mode !== 'mem-file' || !fileHandle) throw new Error('未连接本地 .db 文件');
  const bytes = sqlite3.capi.sqlite3_js_db_export(db);
  const w = await fileHandle.createWritable();
  await w.write(bytes);
  await w.close();
  dirty = false;
  return { size: bytes.byteLength };
}

async function importDbBytes({ bytes, importMode }) {
  // 临时内存库装载导入的 .db,读出全部书签行(含向量),按行合并/替换当前库
  await ensureDb();
  const tmp = new sqlite3.oo1.DB(':memory:', 'ct');
  const img = deWal(bytes);
  const p = sqlite3.wasm.allocFromTypedArray(img);
  try {
    const rc = sqlite3.capi.sqlite3_deserialize(tmp.pointer, 'main', p,
      img.byteLength, img.byteLength, 1 | 4);
    if (rc !== 0) throw new Error('deserialize rc=' + rc);
  } catch (e) {
    sqlite3.wasm.dealloc(p);
    tmp.close();
    throw e;
  }
  const rows = tmp.selectObjects('SELECT * FROM tweet');
  tmp.close();
  if (importMode === 'replace') {
    run('DELETE FROM tweet');
  }
  let added = 0, dup = 0;
  run('BEGIN');  // 万级行单事务,避免逐行 fsync
  try {
    for (const r of rows) {
      if (!r.tweet_id || !r.text) continue;
      const exists = q('SELECT 1 AS x FROM tweet WHERE tweet_id=?', [r.tweet_id]);
      if (exists.length) { dup++; continue; }
      run(`INSERT INTO tweet(tweet_id, text, author_handle, author_name, created_at,
         media_json, urls_json, is_retweet, is_reply, embed_text, embedding,
         embedding_model, embedded_at, imported_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))`,
        [r.tweet_id, r.text, r.author_handle || null, r.author_name || null,
         r.created_at || null, r.media_json || '[]', r.urls_json || '[]',
         r.is_retweet ? 1 : 0, r.is_reply ? 1 : 0, r.embed_text || null,
         r.embedding || null, r.embedding_model || null, r.embedded_at || null]);
      added++;
    }
    run('COMMIT');
  } catch (e) {
    try { run('ROLLBACK'); } catch (e2) { /* ignore */ }
    throw e;
  }
  return { added, dup, total: rows.length };
}

const HANDLERS = {
  async storageInfo() {
    return {
      mode, hasHandle: !!fileHandle,
      fileName: fileHandle ? fileHandle.name : null,
      dirty: mode === 'mem-file' ? dirty : false,
    };
  },
  async setFileHandle({ handle, bytes, name }) {
    fileHandle = handle || null;
    return useFileDb({ bytes, name });
  },
  async useFileDbOp(payload) { return useFileDb(payload || {}); },
  async useOpfsDbOp() { return useOpfsDb(); },
  async saveFile() { return saveFile(); },
  async exportDbBytes() {
    await ensureDb();
    return sqlite3.capi.sqlite3_js_db_export(db);
  },
  async importDbBytes(payload) { return importDbBytes(payload); },
  async vfsInfo() {
    const sqlite3 = await modPromise;
    const find = (name) => {
      try { return sqlite3.capi.sqlite3_vfs_find(name) ? 'yes' : 'no'; }
      catch (e) { return 'err:' + e; }
    };
    let storageDir = 'n/a';
    try {
      const root = await navigator.storage.getDirectory();
      storageDir = root ? 'ok' : 'null';
    } catch (e) { storageDir = 'err:' + e; }
    return {
      disable: sqlite3.config.disable || null,
      isWorker: typeof WorkerGlobalScope !== 'undefined',
      storageDir,
      vfs: { opfs: find('opfs'), sahpool: find('opfs-sahpool'), unix: find('unix'), memdb: find('memdb') },
      logs: logs.slice(-8),
      dbOpenError,
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
    const NO_DB_OPS = ['vfsInfo', 'storageInfo', 'setFileHandle', 'useFileDbOp',
      'useOpfsDbOp', 'saveFile', 'importDbBytes'];
    if (!NO_DB_OPS.includes(op)) await ensureDb();
    const handler = HANDLERS[op];
    if (!handler) throw new Error('unknown op: ' + op);
    const result = await handler(payload || {});
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: String((err && err.message) || err) });
  }
};
