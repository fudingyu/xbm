// xbm static edition — main wiring
import { Store } from './store.js';
import { loadEngine } from './model.js';
import { embedPending, buildEmbedText } from './embedder.js';
import { hybridSearch } from './search.js';
import { bookmarkletHref, COLLECT_URL, COLLECT_ORIGIN } from './collector.js';
import { CATEGORY_DEFS, DOC_PREFIX } from './taxonomy.js';
import { llmConfig, nameCluster, narrative, threadCard } from './llm.js';
import { initStorage, pickNewDb, pickExistingDb, forgetHandle, pickerSupported, connectHandle } from './storage.js';

const $ = (id) => document.getElementById(id);
let store = null;
let engine = null;
let embedRunning = false;

function setStatus(msg) { $('conn').textContent = 'db: ' + msg; }

// ---------- view switching ----------
const VIEWS = ['search', 'browse', 'map', 'timeline', 'threads', 'collect', 'data', 'settings'];
function switchView(name) {
  for (const v of VIEWS) {
    $('view-' + v).classList.toggle('active', v === name);
    $(v + 'view').hidden = v !== name;
  }
  if (name === 'data') refreshData();
  if (name === 'browse') loadBrowseMeta();
  if (name === 'map') loadMapView();
  if (name === 'timeline') loadTimeline();
  if (name === 'threads') loadThreads();
}
VIEWS.forEach((v) => $('view-' + v).addEventListener('click', () => switchView(v)));

// ---------- boot: db ----------
(async () => {
  try {
    store = new Store();
    window.__store = store;  // debug access
    const info = await store.call('info');
    setStatus(`${info.tweets} bookmarks, ${info.embedded} embedded (OPFS, persistent)`);
    // 自动补嵌入(有模型时);无模型则等用户加载后再跑
    tryEmbedLoop();
  } catch (e) {
    setStatus('ERROR — ' + e.message);
  }
})();

// ---------- model ----------
async function loadModel() {
  const type = $('model-source-type').value;
  const btn = $('btn-load-model');
  btn.disabled = true;
  try {
    let source;
    if (type === 'file') {
      const f = $('model-file').files[0];
      if (!f) { setStatus('pick a .litertlm file first'); return; }
      source = f;
    } else {
      source = $('model-url').value.trim();
      if (!source) { setStatus('enter a model URL first'); return; }
    }
    engine = await loadEngine(source, setStatus);
    $('btn-bench').disabled = false;
    tryEmbedLoop();
  } catch (e) {
    setStatus('model load failed — ' + e.message);
  } finally {
    btn.disabled = false;
  }
}
$('btn-load-model').addEventListener('click', loadModel);
$('model-source-type').addEventListener('change', () => {
  const isUrl = $('model-source-type').value === 'url';
  $('model-url').hidden = !isUrl;
  $('model-file').hidden = isUrl;
});

// ---------- embedding loop ----------
let embedQueued = false;
async function tryEmbedLoop() {
  if (!engine || embedRunning) { embedQueued = true; return; }
  embedRunning = true;
  try {
    await embedPending(store, engine, {
      batchSize: 32,
      onProgress: async (n) => {
        setStatus(`embedding… +${n}`);
        const info = await store.call('info');
        setStatus(`${info.tweets} bookmarks, ${info.embedded} embedded (embedding…)`);
      },
    });
    const info = await store.call('info');
    setStatus(`${info.tweets} bookmarks, ${info.embedded} embedded (OPFS, persistent)`);
  } catch (e) {
    setStatus('embedding stopped — ' + e.message);
  } finally {
    embedRunning = false;
    if (embedQueued) { embedQueued = false; tryEmbedLoop(); }
  }
}

// ---------- search ----------
function renderHit(h, el) {
  const div = document.createElement('div');
  div.className = 'hit';
  const badges = [];
  if (h.sim != null) badges.push(`<span class="badge">semantic ${(h.sim * 100).toFixed(0)}%</span>`);
  if (h.fts_hit) badges.push('<span class="badge fts">keyword ✓</span>');
  const date = (h.created_at || '').slice(0, 10);
  const srcUrl = `https://x.com/${h.author_handle || 'i'}/status/${h.tweet_id}`;
  const media = (h.media || []).map((m) =>
    `<img src="${m.url}" loading="lazy" onerror="this.remove()" alt="">`).join('');
  const long = (h.text || '').length > 220;
  div.innerHTML = `
    <div class="meta"><span>${badges.join('')}</span>
      <b>@${h.author_handle || '?'}</b><span>${date}</span>
      <a class="src" href="${srcUrl}" target="_blank" rel="noreferrer">original ↗</a></div>
    <div class="text${long ? ' clamp' : ''}"></div>
    ${long ? '<button class="more ghost">show more</button>' : ''}
    <div class="thumbs">${media}</div>`;
  const tn = div.querySelector('.text');
  tn.textContent = h.text || '';
  const more = div.querySelector('.more');
  if (more) more.addEventListener('click', () => {
    const clamped = tn.classList.toggle('clamp');
    more.textContent = clamped ? 'show more' : 'collapse';
  });
  el.appendChild(div);
}

// renderCards:Browse/复用渲染(字段来自 worker 行,media/urls 为 JSON 字符串)
function renderCards(hits, el) {
  for (const h of hits) {
    let media = [], urls = [];
    try { media = JSON.parse(h.media_json || '[]'); } catch { /* ignore */ }
    try { urls = JSON.parse(h.urls_json || '[]'); } catch { /* ignore */ }
    renderHit({ ...h, media, urls }, el);
  }
}

async function doSearch() {
  const q = $('q').value.trim();
  if (!q || !engine) return;
  const el = $('results');
  el.innerHTML = '<p class="muted">searching…</p>';
  try {
    const hits = await hybridSearch(store, engine, q);
    el.innerHTML = '';
    if (!hits.length) { el.innerHTML = '<p class="muted">(no results)</p>'; return; }
    for (const h of hits) renderHit(h, el);
  } catch (e) {
    el.innerHTML = `<p class="note">search failed: ${e.message}</p>`;
  }
}
$('go').addEventListener('click', doSearch);
$('q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

// ---------- collect ----------
const TEST_MODE = new URLSearchParams(location.search).has('test');
function acceptOrigin(origin) {
  return origin === COLLECT_ORIGIN || (TEST_MODE && origin === location.origin);
}
window.addEventListener('message', async (e) => {
  if (!acceptOrigin(e.origin)) return;
  const d = e.data || {};
  if (d.type === 'xbm-batch' && Array.isArray(d.tweets) && d.tweets.length) {
    const rows = d.tweets.map((r) => ({ ...r, embed_text: buildEmbedText(r.author_handle, r.text) }));
    const r = await store.call('merge', { rows });
    $('collect-progress').textContent =
      `received ${r.added} new / ${r.dup} dup (total merged so far)`;
    tryEmbedLoop();
  } else if (d.type === 'xbm-status') {
    $('collect-progress').textContent = d.msg + (d.done ? ' ✓' : '…');
  } else if (d.type === 'xbm-error') {
    $('collect-progress').textContent = 'collector error: ' + d.error;
  }
});

$('bm-link').href = bookmarkletHref();
$('bm-copy').addEventListener('click', async () => {
  await navigator.clipboard.writeText(decodeURIComponent($('bm-link').href.slice('javascript:'.length)));
  $('collect-note').textContent = 'collector code copied — paste as a bookmark URL if dragging fails';
});
$('btn-open-x').addEventListener('click', () => {
  window.open(COLLECT_URL, 'xbm-collector');
  $('collect-note').textContent = 'x.com opened — click the 📌 xbm collect bookmark there.';
});
$('btn-test-import').addEventListener('click', async () => {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const rows = Array.from({ length: 10 }, (_, i) => {
    const text = `Sample bookmark ${i + 1}: discussion of local LLM inference optimization, WebGPU benchmark notes, and bookmark manager design for X.com data.`;
    return {
      tweet_id: 'test-' + Date.now() + '-' + i,
      text, author_handle: 'sample_user', author_name: 'Sample User',
      created_at: now, media: [], urls: [], is_retweet: false, is_reply: false,
      embed_text: buildEmbedText('sample_user', text),
    };
  });
  const r = await store.call('merge', { rows });
  $('collect-note').textContent = `test import: +${r.added} new / ${r.dup} dup`;
  tryEmbedLoop();
});

// ---------- storage:数据库存储位置(OPFS / 本地 .db 文件) ----------
let saveTimer = null;

async function refreshStorageUI() {
  const info = await store.call('storageInfo');
  const el = $('storage-status');
  if (info.mode === 'file') {
    el.innerHTML = `Mode: <b>local .db file</b> — ${info.fileName}`
      + (info.dirty ? ' <span style="color:#e5a13d">● unsaved changes</span>' : ' ✓ saved');
    $('btn-save-now').hidden = false;
  } else {
    el.textContent = 'Mode: private browser storage (OPFS) — persistent, invisible file, per-origin';
    $('btn-save-now').hidden = true;
  }
  $('btn-reconnect').hidden = !(storageBoot && storageBoot.mode === 'reconnect-needed');
  $('btn-pick-db').disabled = !pickerSupported;
  $('storage-hint').textContent = pickerSupported
    ? 'Tip: pick a .db inside a Google Drive / OneDrive synced folder and you get cloud sync for free (real Drive API may come later). Firefox/Safari: only OPFS is available.'
    : 'This browser cannot pick local files (needs desktop Chrome/Edge) — using private browser storage.';
  return info;
}

let storageBoot = null;

async function initStorageFlow() {
  try {
    storageBoot = await initStorage(store);
    if (storageBoot.mode === 'file') {
      setStatus(`db: local file "${storageBoot.name}" connected`);
      await autoSaveIfDirty();
    } else if (storageBoot.mode === 'reconnect-needed') {
      setStatus(`db: saved file "${storageBoot.name}" found — click Reconnect in Settings`);
    }
  } catch (e) {
    setStatus('storage init failed — ' + e.message);
  }
  await refreshStorageUI();
}

async function autoSaveIfDirty() {
  try {
    const info = await store.call('storageInfo');
    if (info.mode === 'file' && info.dirty) {
      const r = await store.call('saveFile');
      $('storage-status').innerHTML = `Mode: <b>local .db file</b> — ${info.fileName} ✓ saved (${(r.size / 1048576).toFixed(1)}MB)`;
    }
  } catch (e) { /* 下次再存 */ }
}

$('btn-pick-db').addEventListener('click', async () => {
  try {
    const r = await pickNewDb(store);
    $('storage-status').innerHTML =
      `Mode: <b>local .db file</b> — ${r.name}, migrated ${r.tweets} bookmarks ✓`;
    setStatus(`db: local file "${r.name}" (${r.tweets} bookmarks)`);
  } catch (e) {
    if (e.name !== 'AbortError') $('storage-hint').textContent = 'pick failed: ' + e.message;
  }
  refreshStorageUI();
});
$('btn-reconnect').addEventListener('click', async () => {
  if (!storageBoot || storageBoot.mode !== 'reconnect-needed') return;
  try {
    const r = await connectHandle(store, storageBoot.handle);
    storageBoot = { mode: 'file' };
    setStatus(`db: local file "${r.name}" reconnected`);
    await autoSaveIfDirty();
    refreshStorageUI();
  } catch (e) {
    $('storage-hint').textContent = 'reconnect failed: ' + e.message;
  }
});
$('btn-save-now').addEventListener('click', () => autoSaveIfDirty());
$('btn-use-opfs').addEventListener('click', async () => {
  await forgetHandle(store);
  storageBoot = { mode: 'opfs' };
  refreshStorageUI();
});
$('btn-import-db').addEventListener('click', async () => {
  const f = $('import-db-file').files[0];
  if (!f) { $('storage-hint').textContent = 'choose a .db file first'; return; }
  const bytes = new Uint8Array(await f.arrayBuffer());
  try {
    const r = await store.call('importDbBytes',
      { bytes, importMode: $('import-db-mode').value });
    $('storage-hint').textContent =
      `imported: +${r.added} new / ${r.dup} dup (file had ${r.total} rows) — remember Save now if in file mode`;
    refreshData();
    tryEmbedLoop();
  } catch (e) {
    $('storage-hint').textContent = 'import failed: ' + e.message;
  }
});
// 自动保存:脏状态每 30 秒 + 页面隐藏时
setInterval(autoSaveIfDirty, 30000);
window.addEventListener('pagehide', autoSaveIfDirty);
await initStorageFlow();

// ---------- data ----------
async function refreshData() {
  const info = await store.call('info');
  $('data-stats').textContent = `${info.tweets} bookmarks · ${info.embedded} embedded`;
}
$('btn-export').addEventListener('click', async () => {
  const rows = await store.call('dump');
  const blob = new Blob([JSON.stringify({ exported: new Date().toISOString(), rows }, null, 1)],
    { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'xbm-backup-' + new Date().toISOString().slice(0, 10) + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
});
$('btn-wipe').addEventListener('click', async () => {
  if (!confirm('Wipe the entire local database? Export a backup first!')) return;
  await store.call('wipe');
  refreshData();
});

// ---------- settings: LLM + benchmark ----------

// 与服务版设置页同源的开箱预设（xbm-v2 config._PRESET_DEFAULTS）。
// 静态版没有后端可写，配置仍是 localStorage；这里只是快填 + 测连。
const LLM_PRESETS = {
  local: { base: 'http://127.0.0.1:8080/v1', model: 'local' },
  intern: { base: 'https://discovery-api.intern-ai.org.cn/v1', model: 'Atria-Dawn' },
  sensenova: { base: 'https://token.sensenova.cn/v1', model: 'sensenova-6.8-flash-lite' },
};

$('llm-preset').addEventListener('change', () => {
  const key = $('llm-preset').value;
  if (!key) return;
  const p = LLM_PRESETS[key];
  if (!p) return;
  $('llm-base').value = p.base;
  $('llm-model').value = p.model;
  $('llm-note').textContent = `prefilled ${key} — edit, Test, then Save`;
  $('llm-preset').value = '';
});

async function testLlmEndpoint() {
  const base = $('llm-base').value.trim();
  const model = $('llm-model').value.trim() || 'local';
  const note = $('llm-note');
  if (!base) { note.textContent = 'fill in an endpoint URL first'; return; }
  note.textContent = 'testing…';
  const t0 = performance.now();
  try {
    const resp = await fetch(base.replace(/\/$/, '') + '/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model, max_tokens: 32, temperature: 0,
        messages: [{ role: 'user', content: 'reply: OK' }],
      }),
    });
    const ms = Math.round(performance.now() - t0);
    if (!resp.ok) {
      // 401/403 = 端点活着但要 key（静态版不带 key，给出可操作提示）
      const why = (resp.status === 401 || resp.status === 403)
        ? `HTTP ${resp.status} — endpoint alive but needs an API key (proxy it, or use a local llama-server)`
        : `HTTP ${resp.status}`;
      note.textContent = `✗ ${why} (${ms}ms)`;
      return;
    }
    const data = await resp.json().catch(() => ({}));
    const n = data.choices?.length ?? 0;
    note.textContent = `✓ chat endpoint reachable (${n} choice(s), ${ms}ms) — remember Save`;
  } catch (e) {
    note.textContent = `✗ unreachable (${e.name || 'network'}): ${e.message || e}`;
  }
}

$('btn-llm-test').addEventListener('click', testLlmEndpoint);
$('btn-llm-save').addEventListener('click', () => {
  localStorage.setItem('xbm-llm-base', $('llm-base').value.trim());
  localStorage.setItem('xbm-llm-model', $('llm-model').value.trim());
  $('llm-note').textContent = 'saved (browser local)';
});
$('llm-base').value = localStorage.getItem('xbm-llm-base') || '';
$('llm-model').value = localStorage.getItem('xbm-llm-model') || '';

async function runBenchmark() {
  const out = $('bench-out');
  out.textContent = 'running…';
  const mk = (i) => `Sample bookmark ${i}: local LLM inference optimization on consumer GPUs, ` +
    `llama.cpp MTP decode speedup and KV cache tuning notes. 示例书签 ${i}:本地大模型推理优化、` +
    `WebGPU 基准测试与浏览器内嵌数据库设计讨论。`;
  const texts = Array.from({ length: 256 }, (_, i) => mk(i));
  const lines = [];
  for (const bs of [16, 32, 64, 128]) {
    const t0 = performance.now();
    for (let i = 0; i < texts.length; i += bs) {
      await engine.computeEmbeddingBatch(texts.slice(i, i + bs), { normalize: true });
    }
    const rate = texts.length / ((performance.now() - t0) / 1000);
    lines.push(`batch ${bs}: ${rate.toFixed(1)} texts/s`);
    out.textContent = lines.join('\n');
  }
  lines.push(`→ 10,000 bookmarks ≈ ${(10000 / parseFloat(lines[lines.length - 2].match(/([\d.]+) texts/)[1]) / 60).toFixed(0)} min`);
  out.textContent = lines.join('\n');
}
$('btn-bench').addEventListener('click', () => runBenchmark().catch((e) => {
  $('bench-out').textContent = 'benchmark failed: ' + e.message;
}));

// debug access (also used by automated tests)
window.__app = { get engine() { return engine; }, switchView, store: () => store };

// ---------------- Browse(类目 × 关键词簇 × LLM 簇 三维交集) ----------------
const filterSel = { category: null, kw: null, llm: null };
const chipLabel = {};
let browseOffset = 0;

VIEWS.forEach((v) => {
  const b = $('view-' + v);
  if (b && !b.dataset.wired) { b.dataset.wired = '1'; b.addEventListener('click', () => switchView(v)); }
});

async function loadBrowseMeta() {
  try {
    const [cats, clusters] = await Promise.all([
      store.call('categoryInfo'), store.call('clusterList'),
    ]);
    renderChipRow('cat-chips', cats.counts.map((c) => ({ key: c.name, label: c.name, n: c.n, dim: 'category' })),
      'No categories yet — click "Assign categories".');
    const kw = (clusters.keyword || []), llm = (clusters.llm || []);
    $('kw-count').textContent = kw.length ? `(${kw.length})` : '';
    $('llm-count').textContent = llm.length ? `(${llm.length})` : '';
    renderChipRow('kw-chips', kw.slice(0, 60).map((c) => ({ key: c.cluster_id, label: c.name, n: c.size, dim: 'kw' })),
      'Not generated — pick "Keyword clusters" and click Generate clusters.', kw.length);
    renderChipRow('llm-chips', llm.slice(0, 60).map((c) => ({ key: c.cluster_id, label: c.name, n: c.size, dim: 'llm' })),
      'Not generated — pick "LLM clusters" and click Generate clusters.', llm.length);
  } catch (e) { $('browse-note').textContent = 'load failed: ' + e.message; }
}

function renderChipRow(boxId, items, emptyText, totalCount) {
  const box = $(boxId);
  box.innerHTML = '';
  if (!items.length) { box.innerHTML = `<span class="muted">${emptyText}</span>`; return; }
  for (const it of items) {
    chipLabel[`${it.dim}:${it.key}`] = it.label;
    const b = document.createElement('button');
    b.className = 'chip' + (filterSel[it.dim] === it.key ? ' on' : '');
    b.innerHTML = `${it.label} <span class="n">${it.n}</span>`;
    b.addEventListener('click', () => {
      filterSel[it.dim] = filterSel[it.dim] === it.key ? null : it.key;
      loadBrowseMeta(); refreshBrowse();
    });
    box.appendChild(b);
  }
  if (totalCount && totalCount > items.length) {
    box.insertAdjacentHTML('beforeend', `<span class="muted">… ${totalCount} total, showing top ${items.length}</span>`);
  }
}

async function refreshBrowse() {
  browseOffset = 0;
  $('browse-results').innerHTML = '';
  $('btn-more-browse').hidden = true;
  const active = Object.values(filterSel).some((v) => v != null);
  if (!active) { $('browse-header').innerHTML = ''; return; }
  await loadMoreBrowse();
}

async function loadMoreBrowse() {
  const params = new URLSearchParams({ offset: browseOffset, limit: 50 });
  if (filterSel.category != null) params.set('category', filterSel.category);
  if (filterSel.kw != null) params.set('kw', filterSel.kw);
  if (filterSel.llm != null) params.set('llm', filterSel.llm);
  const r = await store.call('browse', Object.fromEntries(params));
  if (browseOffset === 0) {
    const parts = [];
    if (filterSel.category != null) parts.push(`category "${filterSel.category}"`);
    if (filterSel.kw != null) parts.push(`kw "${chipLabel['kw:' + filterSel.kw] ?? '#' + filterSel.kw}"`);
    if (filterSel.llm != null) parts.push(`LLM "${chipLabel['llm:' + filterSel.llm] ?? '#' + filterSel.llm}"`);
    $('browse-header').innerHTML = `<h3>${parts.join(' ∩ ')} — ${r.total} bookmarks</h3>`;
  }
  renderCards(r.hits, $('browse-results'), { withScores: false, webappRows: true });
  browseOffset += r.hits.length;
  $('btn-more-browse').hidden = !r.has_more;
}
$('btn-more-browse').addEventListener('click', loadMoreBrowse);

async function doClassify() {
  const btn = $('btn-classify');
  btn.disabled = true;
  $('browse-note').textContent = 'Embedding category descriptions (WebGPU) + assigning…';
  try {
    if (!engine) throw new Error('load the model first (Settings)');
    const resps = await engine.computeEmbeddingBatch(
      CATEGORY_DEFS.map((d) => DOC_PREFIX + d[1]), { normalize: true });
    const r = await store.call('assignCategories', {
      categories: CATEGORY_DEFS.map((d, i) => ({ name: d[0], vec: Float32Array.from(resps[i].embedding) })),
    });
    $('browse-note').textContent = `Assigned ${r.assigned} bookmarks`;
    loadBrowseMeta();
  } catch (e) {
    $('browse-note').textContent = 'failed: ' + e.message;
  } finally { btn.disabled = false; }
}
$('btn-classify').addEventListener('click', doClassify);

async function doCluster() {
  const btn = $('btn-cluster');
  btn.disabled = true;
  const set = $('cluster-set').value;
  const useLlm = set === 'llm';
  try {
    $('browse-note').textContent = `Clustering (${set}, ${$('cluster-granularity').value})… this can take a minute`;
    const r = await store.call('runClustering', {
      set, granularity: $('cluster-granularity').value,
      recluster: $('cluster-recluster').checked,
    });
    let extra = '';
    if (set === 'llm' && r.clusters) {
      // LLM 命名(可选,失败留空)
      const cfg = llmConfig();
      const unnamed = await store.call('unnamedClusters', { set });
      if (cfg.base && unnamed.length) {
        let ok = 0;
        for (const u of unnamed) {
          const name = await nameCluster(cfg.base, cfg.model, u.texts);
          if (name) { await store.call('setClusterName', { set, cluster_id: u.cluster_id, name }); ok++; }
        }
        extra = `, LLM named ${ok}/${unnamed.length}`;
      } else if (!cfg.base) {
        extra = ' (configure an LLM URL in Settings to auto-name clusters)';
      }
    }
    $('browse-note').textContent = `Done: ${r.clusters} ${set} clusters (${r.noise} unmatched)${extra}`;
    loadBrowseMeta();
  } catch (e) {
    $('browse-note').textContent = 'failed: ' + e.message;
  } finally { btn.disabled = false; }
}

// ---------------- Map(PCA 投影散点) ----------------
const PALETTE = ['#4f8cff','#3fb96f','#e5a13d','#e5735f','#b06fd8','#4fc3d9',
  '#c9db4a','#ff8fab','#8d7bff','#5fd3a2','#f2a54f','#7f9cf5','#d84f7f','#63c984',
  '#a0d8ef','#ffd166','#9be564','#f4845f','#c4bbf0','#78c6a3'];
let mapData = null, mapT = { scale: 1, tx: 0, ty: 0 };

function mapGeom() {
  const c = $('map-canvas');
  return { c, ctx: c.getContext('2d'), W: c.clientWidth, H: c.clientHeight };
}
function mapXY(p, W, H) {
  const s = mapT.scale;
  return [(p.x * W + mapT.tx) * s + W * (1 - s) / 2, (p.y * H + mapT.ty) * s + H * (1 - s) / 2];
}
function drawMap() {
  const { c, ctx, W, H } = mapGeom();
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(W * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!mapData || !mapData.points.length) return;
  const gidx = Object.keys(mapData.groups || {});
  for (const p of mapData.points) {
    const gi = p.g == null ? -1 : gidx.indexOf(String(p.g));
    ctx.fillStyle = p.g == null ? 'rgba(139,147,163,.30)' : PALETTE[Math.max(gi, 0) % PALETTE.length];
    const [x, y] = mapXY(p, W, H);
    ctx.fillRect(x - 1.3, y - 1.3, 2.8, 2.8);
  }
}
async function loadMapView() {
  try {
    if (!mapData) {
      const r = await store.call('mapPoints', { color: $('map-color').value });
      if (!r.points.length) { $('map-note').textContent = 'No projection yet — click "Compute projection"'; }
      mapData = r; mapT = { scale: 1, tx: 0, ty: 0 };
      $('map-note').textContent = `${r.points.length} points · scroll to zoom, drag to pan, click for the original`;
    }
    const box = $('map-legend');
    box.innerHTML = '';
    Object.entries(mapData.groups || {}).slice(0, 26).forEach(([g, label], i) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.innerHTML = `<i style="width:10px;height:10px;border-radius:5px;display:inline-block;background:${PALETTE[i % PALETTE.length]}"></i> ${label}`;
      box.appendChild(chip);
    });
    drawMap();
  } catch (e) { $('map-note').textContent = 'map failed: ' + e.message; }
}
$('map-color').addEventListener('change', () => { mapData = null; if (!$('mapview').hidden) loadMapView(); });
$('btn-map-gen').addEventListener('click', async () => {
  const btn = $('btn-map-gen');
  btn.disabled = true;
  $('map-note').textContent = 'Computing PCA projection… (a few seconds)';
  try {
    const r = await store.call('computeMap');
    $('map-note').textContent = `${r.points} points projected (PCA)`;
    mapData = null;
    loadMapView();
  } catch (e) { $('map-note').textContent = 'failed: ' + e.message; }
  finally { btn.disabled = false; }
});
(() => {
  const c = $('map-canvas');
  let drag = null;
  const nearest = (mx, my) => {
    if (!mapData) return null;
    const { W, H } = mapGeom();
    let best = null, bd = 100;
    for (const p of mapData.points) {
      const [x, y] = mapXY(p, W, H);
      const d = (x - mx) ** 2 + (y - my) ** 2;
      if (d < bd) { bd = d; best = p; }
    }
    return best;
  };
  const groupOf = (g) => g == null ? 'ungrouped' : (mapData?.groups[String(g)] || '#' + g);
  c.addEventListener('mousedown', (e) => { drag = { x: e.offsetX, y: e.offsetY, tx: mapT.tx, ty: mapT.ty, moved: false }; });
  c.addEventListener('mousemove', (e) => {
    if (drag) {
      const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      mapT.tx = drag.tx + dx; mapT.ty = drag.ty + dy;
      $('map-tip').hidden = true; drawMap(); return;
    }
    const best = nearest(e.offsetX, e.offsetY);
    const tip = $('map-tip');
    if (best) {
      tip.hidden = false; tip.style.left = (e.offsetX + 14) + 'px'; tip.style.top = (e.offsetY + 10) + 'px';
      tip.textContent = `[${groupOf(best.g)}] ${best.t}`;
    } else tip.hidden = true;
  });
  c.addEventListener('mouseleave', () => { $('map-tip').hidden = true; drag = null; });
  c.addEventListener('mouseup', (e) => {
    const wasDrag = drag && drag.moved; drag = null;
    if (wasDrag) return;
    const best = nearest(e.offsetX, e.offsetY);
    if (best) window.open(`https://x.com/i/status/${best.id}`, '_blank');
  });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    mapT.scale = Math.min(30, Math.max(0.5, mapT.scale * f));
    drawMap();
  }, { passive: false });
  window.addEventListener('resize', () => { if (!$('mapview').hidden) drawMap(); });
})();

// ---------------- Timeline ----------------
async function loadTimeline() {
  try {
    const digest = await store.call('timelineDigest',
      { unit: $('timeline-unit').value, window: $('timeline-window').value });
    const key = `xbm-timeline-${$('timeline-unit').value}-${$('timeline-window').value}`;
    renderTimeline(digest, localStorage.getItem(key));
  } catch (e) { $('timeline-note').textContent = 'failed: ' + e.message; }
}
function renderTimeline(digest, narrative) {
  const narr = $('timeline-narrative'), box = $('timeline-digest');
  if (narrative) narr.textContent = narrative;
  else narr.innerHTML = '<span class="muted">(no LLM narrative yet — Generate works without an LLM for stats)</span>';
  box.innerHTML = '';
  for (const d of digest || []) {
    const div = document.createElement('div');
    div.className = 'tl-period';
    const cats = (d.cats || []).map(([k, v]) => `${k} ${v}`).join(', ');
    const llm = (d.llm || []).map(([k, v]) => `${k} ${v}`).join(', ');
    const kw = (d.kw || []).map(([k, v]) => `${k} ${v}`).join(', ');
    div.innerHTML = `<b>${d.period}</b>${d.total} bookmarks`
      + `<div class="muted">categories: ${cats || '-'}<br>topics: ${llm || '-'}<br>keywords: ${kw || '-'}</div>`
      + (d.samples || []).map((s) => `<div class="muted">e.g. “${s}”</div>`).join('');
    box.appendChild(div);
  }
}
$('btn-timeline').addEventListener('click', async () => {
  const btn = $('btn-timeline');
  btn.disabled = true;
  const unit = $('timeline-unit').value, win = $('timeline-window').value;
  try {
    const digest = await store.call('timelineDigest', { unit, window: win });
    renderTimeline(digest, null);
    const cfg = llmConfig();
    if (!cfg.base) {
      $('timeline-note').textContent = `${digest.length} periods (configure an LLM URL in Settings for the narrative)`;
      return;
    }
    $('timeline-note').textContent = `LLM writing narrative for ${digest.length} periods… (minutes)`;
    const lines = digest.map((d) => {
      const s = d.llm.length ? d.llm.map(([k, v]) => `${k}${v}`).join('/')
        : d.cats.map(([k, v]) => `${k}${v}`).join('/');
      return `${d.period}(${d.total}) ${s}`;
    }).join('\n');
    const narrativeText = await narrative(cfg.base, cfg.model, lines);
    localStorage.setItem(`xbm-timeline-${unit}-${win}`, narrativeText);
    renderTimeline(digest, narrativeText);
    $('timeline-note').textContent = `${digest.length} periods, narrative generated`;
  } catch (e) {
    $('timeline-note').textContent = 'narrative failed: ' + e.message + ' (stats remain valid)';
  } finally { btn.disabled = false; }
});
$('timeline-unit').addEventListener('change', loadTimeline);
$('timeline-window').addEventListener('change', loadTimeline);

// ---------------- Threads ----------------
const SEC_META = {
  dormant: ['⚠ Dormant threads', 'invested, then starved — review and restart these', true],
  persistent: ['✓ Persistent threads', 'recurring systematic interests (knowledge assets)', false],
  emerging: ['🌱 Emerging', 'first seen recently — worth watching', false],
  pulse: ['⚡ Pulses', 'short bursts (algorithm hot topics) — skim and move on', false],
};
async function loadThreads() {
  try {
    const threads = await store.call('threadProfilesOp');
    const cards = JSON.parse(localStorage.getItem('xbm-thread-cards') || '{}');
    const box = $('threads-body');
    box.innerHTML = '';
    for (const cls of ['dormant', 'persistent', 'emerging', 'pulse']) {
      const list = threads[cls] || [];
      const [title, sub, warn] = SEC_META[cls];
      const sec = document.createElement('div');
      sec.innerHTML = `<div class="thread-sec"><b>${title}</b> (${list.length}) <span class="muted">${sub}</span></div>`;
      box.appendChild(sec);
      for (const t of list.slice(0, cls === 'pulse' ? 15 : 40)) {
        const div = document.createElement('div');
        div.className = 'thread-card' + (cls === 'dormant' ? ' dormant' : '');
        const card = cards[`${t.set}:${t.cluster_id}`];
        const stats = [`${t.size} bookmarks`, `active ${t.active_months} months`,
          `${t.first} ~ ${t.last}`,
          cls === 'dormant' ? `<span class="warn">dormant ${t.dormancy} months</span>` : '',
          t.recent3 ? `+${t.recent3} last 3mo` : ''].filter(Boolean).join(' · ');
        div.innerHTML = `
          <div class="head"><b data-set="${t.set}" data-id="${t.cluster_id}">${t.name}</b>
            <span class="spark">${t.spark}</span></div>
          <div class="stats">${stats}</div>
          ${card ? `<div class="cardllm"><div><span class="lbl">What</span>${card.about || ''}</div>
            <div><span class="lbl">Next</span>${card.next || ''}</div></div>` : ''}`;
        div.querySelector('b').addEventListener('click', () => {
          switchView('browse');
          filterSel.category = null;
          filterSel.kw = t.set === 'keyword' ? t.cluster_id : null;
          filterSel.llm = t.set === 'llm' ? t.cluster_id : null;
          loadBrowseMeta(); refreshBrowse();
        });
        box.appendChild(div);
      }
    }
  } catch (e) { $('threads-note').textContent = 'failed: ' + e.message; }
}
$('btn-thread-cards').addEventListener('click', async () => {
  const btn = $('btn-thread-cards');
  btn.disabled = true;
  try {
    const cfg = llmConfig();
    if (!cfg.base) throw new Error('configure an LLM URL in Settings first');
    const threads = await store.call('threadProfilesOp');
    const targets = threads.dormant.slice(0, 6).concat(threads.persistent.slice(0, 6));
    const cards = JSON.parse(localStorage.getItem('xbm-thread-cards') || '{}');
    let ok = 0;
    for (let i = 0; i < targets.length; i++) {
      const t = targets[i];
      $('threads-note').textContent = `writing cards ${i + 1}/${targets.length}…`;
      const samples = await store.call('threadSamples', { set: t.set, cluster_id: t.cluster_id });
      const prompt = `Thread "${t.name}": ${t.size} bookmarks, active ${t.active_months} months (${t.first} ~ ${t.last})`
        + (t.class === 'dormant' ? `, dormant for ${t.dormancy} months` : '')
        + '\nRepresentative content:\n' + samples.map((s) => '- ' + s).join('\n');
      const card = await threadCard(cfg.base, cfg.model, prompt);
      if (card) { cards[`${t.set}:${t.cluster_id}`] = card; ok++; }
      localStorage.setItem('xbm-thread-cards', JSON.stringify(cards));
    }
    $('threads-note').textContent = `cards written ${ok}/${targets.length}`;
    loadThreads();
  } catch (e) {
    $('threads-note').textContent = 'failed: ' + e.message;
  } finally { btn.disabled = false; }
});

// JSON 导入
$('btn-import-json').addEventListener('click', async () => {
  const f = $('import-json-file').files[0];
  if (!f) { $('storage-hint').textContent = 'choose a JSON backup first'; return; }
  try {
    const data = JSON.parse(await f.text());
    const r = await store.call('importJsonRows', { rows: data.rows || [] });
    $('storage-hint').textContent = `JSON imported: +${r.added} new / ${r.dup} dup`;
    refreshData(); tryEmbedLoop();
  } catch (e) { $('storage-hint').textContent = 'JSON import failed: ' + e.message; }
});
