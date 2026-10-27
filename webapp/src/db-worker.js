// db-worker.js — SQLite WASM(opfs-sahpool VFS)专属 worker。
// sahpool 用同步文件句柄(仅 worker 可用),持久化在浏览器 OPFS;同一时刻只允许一个连接。
import sqlite3InitModule from '@sqlite.org/sqlite-wasm';
import { tokenize, leaderDense, leaderSparse, pca2, threadProfiles,
         GRANULARITY_LLM, GRANULARITY_KW, DOC_PREFIX } from './taxonomy.js';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS tweet(
  tweet_id      TEXT PRIMARY KEY,
  text          TEXT NOT NULL DEFAULT '',
  author_handle TEXT, author_name TEXT, created_at TEXT,
  media_json    TEXT NOT NULL DEFAULT '[]',
  urls_json     TEXT NOT NULL DEFAULT '[]',
  is_retweet    INTEGER DEFAULT 0, is_reply INTEGER DEFAULT 0,
  embed_text    TEXT, embedding BLOB, embedding_model TEXT,
  embedded_at   TEXT, imported_at   TEXT,
  category      TEXT, category_score REAL,
  cluster_keyword INTEGER, cluster_llm INTEGER,
  map_x REAL, map_y REAL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cluster(
  set_name TEXT NOT NULL, cluster_id INTEGER NOT NULL, name TEXT,
  size INTEGER, sample_json TEXT, centroid_json TEXT, updated_at TEXT,
  PRIMARY KEY(set_name, cluster_id)
);
`;

// 已存在的库(如从 Python 版导入的)补齐 M3/M4 列
function ensureColumns() {
  const cols = q('PRAGMA table_info(tweet)').map((r) => r.name);
  const need = [['category', 'TEXT'], ['category_score', 'REAL'],
    ['cluster_keyword', 'INTEGER'], ['cluster_llm', 'INTEGER'],
    ['map_x', 'REAL'], ['map_y', 'REAL']];
  for (const [col, ddl] of need) {
    if (!cols.includes(col)) run(`ALTER TABLE tweet ADD COLUMN ${col} ${ddl}`);
  }
  const ccols = q('PRAGMA table_info(cluster)').map((r) => r.name);
  if (!ccols.length) run(`CREATE TABLE IF NOT EXISTS cluster(
      set_name TEXT NOT NULL, cluster_id INTEGER NOT NULL, name TEXT,
      size INTEGER, sample_json TEXT, centroid_json TEXT, updated_at TEXT,
      PRIMARY KEY(set_name, cluster_id))`);
}

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
    ensureColumns();
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
  db.exec(SCHEMA);
  ensureColumns();
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
  ensureColumns();
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

function loadVecs() {
    const rows = q(`SELECT tweet_id, embedding, category, cluster_keyword, cluster_llm,
                           created_at, text FROM tweet WHERE LENGTH(embedding) > 0`);
    const n = rows.length;
    const d = n ? rows[0].embedding.byteLength / 4 : 0;
    const data = new Float32Array(n * d);
    for (let i = 0; i < n; i++) {
      data.set(new Float32Array(rows[i].embedding.buffer,
        rows[i].embedding.byteOffset, d), i * d);
    }
    return { rows, M: { data, n, d } };
}

function saveClusterSet(set, clusters) {
    run(`DELETE FROM cluster WHERE set_name=?`, [set]);
    for (const c of clusters) {
      run(`INSERT INTO cluster(set_name, cluster_id, name, size, sample_json, centroid_json, updated_at)
           VALUES(?,?,?,?,?,?,datetime('now'))`,
        [set, c.cid, c.name ?? null, c.size,
         JSON.stringify(c.sample || []), JSON.stringify(c.centroid || [])]);
    }
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

  // ---------- 分析:类目 / 聚类 / 浏览 / 地图 / 时间线 / 线索(Python 移植) ----------



  runClustering({ set = 'keyword', granularity = 'medium' }) {
    ensureColumns();
    const thr = (set === 'keyword' ? GRANULARITY_KW : GRANULARITY_LLM)[granularity] ?? 0.65;
    if (set === 'keyword') {
      // 文本视角:token(CJK bigram + EN 词)→ IDF 稀疏向量 → 引领聚类
      const rows = q(`SELECT tweet_id, text FROM tweet WHERE LENGTH(embedding) > 0 AND text != ''`);
      const n = rows.length;
      const tokenLists = rows.map((r) => [...tokenize(r.text)]);
      const df = new Map();
      for (const toks of tokenLists) for (const t of toks) df.set(t, (df.get(t) || 0) + 1);
      const dfMin = n < 500 ? 1 : 3, dfMax = n * 0.30;
      const items = [];
      for (let i = 0; i < n; i++) {
        const vec = new Map();
        for (const t of tokenLists[i]) {
          const d0 = df.get(t);
          if (d0 >= dfMin && d0 <= dfMax) vec.set(t, Math.log(1 + n / d0));
        }
        let nrm = 0; for (const w of vec.values()) nrm += w * w;
        nrm = Math.sqrt(nrm) || 1;
        for (const [t, w] of vec) vec.set(t, w / nrm);
        items.push({ idx: i, vec });
      }
      const labels = leaderSparse(items, thr);
      const sizes = new Map();
      for (const l of labels.values()) sizes.set(l, (sizes.get(l) || 0) + 1);
      const minKeep = Math.max(1, Math.min(4, Math.floor(n / 10)));
      const remap = new Map(); let next = 0;
      for (const [l, s] of [...sizes.entries()].sort((a, b) => b[1] - a[1])) {
        if (s >= minKeep) remap.set(l, next++);
      }
      const members = new Map();
      for (const [i, l] of labels) {
        const nl = remap.get(l);
        if (nl === undefined) continue;
        if (!members.has(nl)) members.set(nl, []);
        members.get(nl).push(rows[i].tweet_id);
      }
      const clusters = [];
      const rowById = new Map(rows.map((r, i) => [r.tweet_id, i]));
      for (const [cid, ids] of members) {
        const freq = new Map();
        for (const tid of ids.slice(0, 50)) {
          const i2 = rowById.get(tid);
          if (i2 === undefined) continue;
          for (const t of items[i2].vec.keys()) freq.set(t, (freq.get(t) || 0) + 1);
        }
        const scored = [...freq.entries()].map(([w, c]) => [w, c / ((df.get(w) || 0) + 3)]);
        scored.sort((a, b) => b[1] - a[1]);
        const name = scored.slice(0, 3).map(([w]) => w).join('·') || ('cluster#' + cid);
        clusters.push({ cid, name, size: ids.length, sample: ids.slice(0, 8) });
      }
      saveClusterSet('keyword', clusters);
      run('UPDATE tweet SET cluster_keyword=NULL');
      const upd = [];
      for (const [i, l] of labels) {
        const nl = remap.get(l);
        if (nl !== undefined) upd.push([nl, rows[i].tweet_id]);
      }
      run('BEGIN');
      for (const [cid, tid] of upd) run('UPDATE tweet SET cluster_keyword=? WHERE tweet_id=?', [cid, tid]);
      run('COMMIT');
      return { method: 'tokens-idf', clusters: clusters.length, noise: n - upd.length };
    }
    // LLM 分区:抽样引领聚类 + 全量质心归并(≥0.60)
    const { rows, M } = loadVecs();
    if (!rows.length) return { method: 'embedding-leader', clusters: 0, noise: 0 };
    const chosen = new Set();
    const nSample = Math.min(4000, rows.length);
    while (chosen.size < nSample) chosen.add(Math.floor(Math.random() * rows.length));
    const sampleRows = [...chosen].map((i) => ({
      tweet_id: rows[i].tweet_id,
      vec: M.data.subarray(i * M.d, (i + 1) * M.d),
    }));
    const slab = leaderDense(sampleRows, thr);
    const sizes = new Map();
    for (const l of slab) if (l >= 0) sizes.set(l, (sizes.get(l) || 0) + 1);
    const clusters = []; const cents = [];
    for (const [l, s] of [...sizes.entries()].sort((a, b) => b[1] - a[1])) {
      if (s < 20) continue;
      const cid = clusters.length;
      const memberIdx = [...chosen].filter((_, k) => slab[k] === l);
      const centroid = new Float32Array(M.d);
      for (const i of memberIdx) for (let k = 0; k < M.d; k++) centroid[k] += M.data[i * M.d + k];
      for (let k = 0; k < M.d; k++) centroid[k] /= memberIdx.length || 1;
      let nrm = Math.sqrt(centroid.reduce((a, x) => a + x * x, 0)) || 1;
      for (let k = 0; k < M.d; k++) centroid[k] /= nrm;
      clusters.push({ cid, name: null, size: memberIdx.length,
        sample: memberIdx.slice(0, 8).map((i) => rows[i].tweet_id), centroid: Array.from(centroid) });
      cents.push(centroid);
    }
    let assigned = 0, unmatched = 0;
    if (cents.length) {
      run('UPDATE tweet SET cluster_llm=NULL');
      const upd = [];
      for (let i = 0; i < rows.length; i++) {
        let best = -1, bestSim = 0.60;
        for (let c = 0; c < cents.length; c++) {
          let s = 0;
          for (let k = 0; k < M.d; k++) s += M.data[i * M.d + k] * cents[c][k];
          if (s > bestSim) { best = c; bestSim = s; }
        }
        if (best >= 0) { upd.push([best, rows[i].tweet_id]); assigned++; }
        else unmatched++;
      }
      run('BEGIN');
      for (const [cid, tid] of upd) run('UPDATE tweet SET cluster_llm=? WHERE tweet_id=?', [cid, tid]);
      run('COMMIT');
    }
    saveClusterSet('llm', clusters);
    return { method: 'embedding-leader', clusters: clusters.length, noise: unmatched };
  },

  unnamedClusters({ set }) {
    return q(`SELECT cluster_id, sample_json FROM cluster WHERE set_name=? AND name IS NULL ORDER BY size DESC`, [set])
      .map((r) => ({ cluster_id: r.cluster_id, texts: JSON.parse(r.sample_json || '[]') }));
  },

  setClusterName({ set, cluster_id, name }) {
    run(`UPDATE cluster SET name=?, updated_at=datetime('now') WHERE set_name=? AND cluster_id=?`,
      [name, set, cluster_id]);
    return true;
  },

  // 类目零样本:categories [{name, vec}] 全量余弦 top-1;向量持久化 meta
  assignCategories({ categories }) {
    ensureColumns();
    if (!categories.length) return { assigned: 0, distribution: {} };
    const C = categories.map((c) => Float32Array.from(c.vec));
    const { rows, M } = loadVecs();
    const updates = []; const dist = {};
    for (let i = 0; i < rows.length; i++) {
      let best = -1, bestSim = -2;
      for (let c = 0; c < C.length; c++) {
        let s = 0;
        for (let k = 0; k < M.d; k++) s += M.data[i * M.d + k] * C[c][k];
        if (s > bestSim) { best = c; bestSim = s; }
      }
      const name = categories[best].name;
      dist[name] = (dist[name] || 0) + 1;
      updates.push([name, bestSim, rows[i].tweet_id]);
    }
    run('BEGIN');
    for (const [cat, sc, tid] of updates) run('UPDATE tweet SET category=?, category_score=? WHERE tweet_id=?', [cat, sc, tid]);
    run('COMMIT');
    run(`INSERT OR REPLACE INTO meta(key, value) VALUES('category_vecs', ?)`,
      [JSON.stringify(categories.map((c) => ({ name: c.name, vec: Array.from(Float32Array.from(c.vec)) })))]);
    return { assigned: rows.length, distribution: dist };
  },

  // 增量归位:类目向量(meta)+ 双分区簇心 → 未归类行
  assignMissingAll() {
    ensureColumns();
    let catAssigned = 0;
    const cvRow = q(`SELECT value FROM meta WHERE key='category_vecs'`)[0];
    if (cvRow) {
      const cats = JSON.parse(cvRow.value);
      const pend = q(`SELECT tweet_id, embedding FROM tweet WHERE LENGTH(embedding)>0 AND category IS NULL`);
      const upd = [];
      for (const r of pend) {
        const v = new Float32Array(r.embedding.buffer, r.embedding.byteOffset, r.embedding.byteLength / 4);
        let best = -1, bestSim = -2;
        for (let c = 0; c < cats.length; c++) {
          const cv = Float32Array.from(cats[c].vec);
          let s = 0; for (let k = 0; k < v.length; k++) s += v[k] * cv[k];
          if (s > bestSim) { best = c; bestSim = s; }
        }
        if (best >= 0) upd.push([cats[best].name, r.tweet_id]);
      }
      run('BEGIN');
      for (const [cat, tid] of upd) run('UPDATE tweet SET category=? WHERE tweet_id=?', [cat, tid]);
      run('COMMIT');
      catAssigned = upd.length;
    }
    let kwAssigned = 0, llmAssigned = 0;
    for (const [set, col] of [['keyword', 'cluster_keyword'], ['llm', 'cluster_llm']]) {
      const crows = q(`SELECT cluster_id, centroid_json FROM cluster WHERE set_name=? AND centroid_json IS NOT NULL`, [set]);
      if (!crows.length) continue;
      const pend = q(`SELECT tweet_id, embedding FROM tweet WHERE LENGTH(embedding)>0 AND ${col} IS NULL`);
      const upd = [];
      for (const r of pend) {
        const v = new Float32Array(r.embedding.buffer, r.embedding.byteOffset, r.embedding.byteLength / 4);
        let best = -1, bestSim = 0.60;
        for (const cr of crows) {
          const c = Float32Array.from(JSON.parse(cr.centroid_json));
          let s = 0; for (let k = 0; k < v.length; k++) s += v[k] * c[k];
          if (s > bestSim) { best = cr.cluster_id; bestSim = s; }
        }
        if (best >= 0) upd.push([best, r.tweet_id]);
      }
      run('BEGIN');
      for (const [cid, tid] of upd) run(`UPDATE tweet SET ${col}=? WHERE tweet_id=?`, [cid, tid]);
      run('COMMIT');
      if (set === 'keyword') kwAssigned = upd.length; else llmAssigned = upd.length;
    }
    return { catAssigned, kwAssigned, llmAssigned };
  },

  browse({ category, kw, llm, offset = 0, limit = 50 }) {
    ensureColumns();
    const conds = [], args = [];
    if (category != null && category !== '') { conds.push('t.category = ?'); args.push(category); }
    if (kw != null && kw !== '') { conds.push('t.cluster_keyword = ?'); args.push(+kw); }
    if (llm != null && llm !== '') { conds.push('t.cluster_llm = ?'); args.push(+llm); }
    if (!conds.length) return { hits: [], has_more: false, total: 0 };
    const rows = q(`SELECT t.*, COUNT(*) OVER() AS total
                     FROM tweet t WHERE ${conds.join(' AND ')}
                     ORDER BY t.created_at DESC LIMIT ? OFFSET ?`,
      [...args, limit + 1, offset]);
    const total = rows.length ? rows[0].total : 0;
    const hits = rows.slice(0, limit);
    hits.forEach((h) => { delete h.total; });
    return { hits, has_more: rows.length > limit, total };
  },

  timelineDigest({ unit = 'month', window = 'all' }) {
    ensureColumns();
    const rows = q(`SELECT text, category, cluster_keyword, cluster_llm, created_at
                    FROM tweet WHERE text != '' AND created_at IS NOT NULL ORDER BY created_at`);
    const kwNames = {}, llmNames = {};
    for (const r of q(`SELECT set_name, cluster_id, COALESCE(name,'?') AS name FROM cluster`)) {
      if (r.set_name === 'keyword') kwNames[r.cluster_id] = r.name;
      else llmNames[r.cluster_id] = r.name;
    }
    const buckets = new Map();
    for (const r of rows) {
      const d = r.created_at;
      const period = unit === 'quarter'
        ? `${d.slice(0, 4)}Q${Math.floor((+d.slice(5, 7) - 1) / 3) + 1}` : d.slice(0, 7);
      let b = buckets.get(period);
      if (!b) { b = { period, total: 0, cats: new Map(), llm: new Map(), kw: new Map(), samples: [] }; buckets.set(period, b); }
      b.total++;
      if (r.category) b.cats.set(r.category, (b.cats.get(r.category) || 0) + 1);
      if (r.cluster_llm != null) { const nm = llmNames[r.cluster_llm] || '?'; b.llm.set(nm, (b.llm.get(nm) || 0) + 1); }
      if (r.cluster_keyword != null) { const nm = kwNames[r.cluster_keyword] || '?'; b.kw.set(nm, (b.kw.get(nm) || 0) + 1); }
      if (b.samples.length < 2) b.samples.push(r.text.slice(0, 48));
    }
    const nowY = new Date().getUTCFullYear(), nowM = new Date().getUTCMonth() + 1;
    const cutoff = window === '12' ? `${nowY - 1}-${String(nowM).padStart(2, '0')}`
      : window === '24' ? `${nowY - 2}-${String(nowM).padStart(2, '0')}` : null;
    const top = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, v]) => [k, v]);
    return [...buckets.entries()].sort((a, b) => a[0].localeCompare(b[0]))
      .filter(([p]) => !cutoff || p >= cutoff)
      .map(([p, b]) => ({ period: p, total: b.total, cats: top(b.cats), llm: top(b.llm), kw: top(b.kw), samples: b.samples }));
  },

  threadProfilesOp({ now }) {
    ensureColumns();
    const nowM = now || new Date().toISOString().slice(0, 7);
    const rows = q(`SELECT cluster_llm, cluster_keyword, created_at FROM tweet WHERE created_at IS NOT NULL`);
    const names = {};
    for (const r of q(`SELECT set_name, cluster_id, COALESCE(name,'') AS name FROM cluster`)) {
      names[r.set_name + ':' + r.cluster_id] = r.name;
    }
    const raw = [];
    for (const r of rows) {
      const month = r.created_at.slice(0, 7);
      if (r.cluster_llm != null) raw.push({ set: 'llm', cid: r.cluster_llm, month });
      if (r.cluster_keyword != null) raw.push({ set: 'keyword', cid: r.cluster_keyword, month });
    }
    return threadProfiles(raw, names, nowM);
  },

  threadSamples({ set, cluster_id }) {
    const r = q(`SELECT sample_json FROM cluster WHERE set_name=? AND cluster_id=?`, [set, cluster_id])[0];
    const ids = r && r.sample_json ? JSON.parse(r.sample_json) : [];
    const out = [];
    for (const tid of ids.slice(0, 6)) {
      const t = q(`SELECT text FROM tweet WHERE tweet_id=?`, [tid])[0];
      if (t && t.text) out.push(t.text.slice(0, 80).replace(/\n/g, ' '));
    }
    return out;
  },

  computeMap() {
    ensureColumns();
    const { rows, M } = loadVecs();
    if (!rows.length) return { points: 0, method: 'none' };
    let coords;
    try { coords = pca2(M); }
    catch (e) { coords = { x: new Float64Array(rows.length), y: new Float64Array(rows.length) }; }
    const xs = coords.x, ys = coords.y;
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    const ymin = Math.min(...ys), ymax = Math.max(...ys);
    const sx = (xmax - xmin) || 1, sy = (ymax - ymin) || 1;
    run('BEGIN');
    for (let i = 0; i < rows.length; i++) {
      run('UPDATE tweet SET map_x=?, map_y=? WHERE tweet_id=?',
        [(xs[i] - xmin) / sx, (ys[i] - ymin) / sy, rows[i].tweet_id]);
    }
    run('COMMIT');
    return { points: rows.length, method: 'pca' };
  },

  mapPoints({ color = 'category' }) {
    ensureColumns();
    const groups = {}; const glist = [];
    let rows;
    if (color === 'category') {
      for (const r of q(`SELECT DISTINCT category FROM tweet WHERE category IS NOT NULL ORDER BY category`)) {
        glist.push(r.category);
      }
      glist.forEach((g, i) => { groups[i] = g; });
      rows = q(`SELECT tweet_id, map_x, map_y, category AS g, text FROM tweet WHERE map_x IS NOT NULL`);
    } else {
      const set = color === 'kw' ? 'keyword' : 'llm';
      const col = color === 'kw' ? 'cluster_keyword' : 'cluster_llm';
      const names = {};
      for (const r of q(`SELECT cluster_id, COALESCE(name,'') AS name FROM cluster WHERE set_name=?`, [set])) {
        names[r.cluster_id] = r.name || ('#' + r.cluster_id);
      }
      for (const r of q(`SELECT cluster_id FROM cluster WHERE set_name=?`, [set])) {
        glist.push(r.cluster_id); groups[r.cluster_id] = names[r.cluster_id];
      }
      rows = q(`SELECT tweet_id, map_x, map_y, ${col} AS g, text FROM tweet WHERE map_x IS NOT NULL`);
    }
    return {
      points: rows.map((r) => ({ id: r.tweet_id, x: r.map_x, y: r.map_y, g: r.g, t: (r.text || '').slice(0, 60) })),
      groups,
    };
  },

  importJsonRows({ rows }) {
    ensureColumns();
    let added = 0, dup = 0;
    run('BEGIN');
    try {
      for (const r of rows) {
        if (!r.tweet_id || !r.text) continue;
        const ex = q('SELECT 1 AS x FROM tweet WHERE tweet_id=?', [r.tweet_id]);
        if (ex.length) { dup++; continue; }
        run(`INSERT INTO tweet(tweet_id, text, author_handle, author_name, created_at,
           media_json, urls_json, is_retweet, is_reply, embed_text, embedding,
           embedding_model, embedded_at, imported_at, category, category_score)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?)`,
          [r.tweet_id, r.text, r.author_handle || null, r.author_name || null,
           r.created_at || null, r.media_json || '[]', r.urls_json || '[]',
           r.is_retweet ? 1 : 0, r.is_reply ? 1 : 0, r.embed_text || null,
           r.embedding || null, r.embedding_model || null, r.embedded_at || null,
           r.category || null, r.category_score || null]);
        added++;
      }
      run('COMMIT');
    } catch (e) { try { run('ROLLBACK'); } catch {} throw e; }
    return { added, dup, total: rows.length };
  },

  categoryInfo() {
    ensureColumns();
    const counts = q(`SELECT category AS name, COUNT(*) AS n FROM tweet
                      WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC`);
    const un = q(`SELECT COUNT(*) AS n FROM tweet WHERE LENGTH(embedding)>0 AND category IS NULL`)[0].n;
    const hasVecs = !!q(`SELECT value FROM meta WHERE key='category_vecs'`)[0];
    return { counts, unclassified: un, hasVecs };
  },

  clusterList() {
    ensureColumns();
    const out = { keyword: [], llm: [] };
    for (const r of q(`SELECT set_name, cluster_id, name, size FROM cluster ORDER BY size DESC`)) {
      (out[r.set_name] = out[r.set_name] || []).push({
        cluster_id: r.cluster_id, name: r.name || ('#' + r.cluster_id),
        raw_name: r.name, size: r.size,
      });
    }
    return out;
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
      'useOpfsDbOp', 'saveFile', 'importDbBytes', 'exportDbBytes'];
    if (!NO_DB_OPS.includes(op)) await ensureDb();
    const handler = HANDLERS[op];
    if (!handler) throw new Error('unknown op: ' + op);
    const result = await handler(payload || {});
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: String((err && err.message) || err) });
  }
};
