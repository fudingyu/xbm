// xbm static edition — main wiring
import { Store } from './store.js';
import { loadEngine } from './model.js';
import { embedPending, buildEmbedText, QUERY_PREFIX } from './embedder.js';
import { hybridSearch } from './search.js';
import { bookmarkletHref, COLLECT_URL, COLLECT_ORIGIN } from './collector.js';

const $ = (id) => document.getElementById(id);
let store = null;
let engine = null;
let embedRunning = false;

function setStatus(msg) { $('conn').textContent = 'db: ' + msg; }

// ---------- view switching ----------
const VIEWS = ['search', 'collect', 'data', 'settings'];
function switchView(name) {
  for (const v of VIEWS) {
    $('view-' + v).classList.toggle('active', v === name);
    $(v + 'view').hidden = v !== name;
  }
  if (name === 'data') refreshData();
}
VIEWS.forEach((v) => $('view-' + v).addEventListener('click', () => switchView(v)));

// ---------- boot: db ----------
(async () => {
  try {
    store = new Store();
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
