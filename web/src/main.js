import { t, setLang, lang } from './i18n.js';
// xbm 前端:浏览器 WebGPU 嵌入(@litert-lm/core)+ 混合检索 UI。
// 用法对应官方示例 js/apps/embedding_search:
//   getOrLoadGlobalLiteRtLm('./wasm') → EmbeddingEngine.create({model: stream, backend: Backend.GPU})
//   engine.computeEmbedding(text, {normalize: true})
import { Backend, EmbeddingEngine, getOrLoadGlobalLiteRtLm } from '@litert-lm/core';

const $ = (id) => document.getElementById(id);
let engine = null;
let cfg = { query_prefix: 'task: search result | query: ', doc_prefix: 'title: none | text: ', model_name: '' };
let indexing = false;
let pauseIndexing = false;  // 用户触发的操作(分类/搜索)优先,索引循环在批边界让位

function setStatus(msg) { $('status').textContent = msg; }

function trackProgress(stream, total, cb) {
  if (!stream) return stream;
  let loaded = 0;
  return new ReadableStream({
    async start(controller) {
      const reader = stream.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        loaded += value.byteLength;
        cb(loaded, total);
        controller.enqueue(value);
      }
      controller.close();
    },
  });
}


function applyI18n() {
  document.title = t('app.title');
  document.getElementById('app-title').textContent = t('app.title');
  document.querySelectorAll('[data-i18n]').forEach(el => {
    // 保留子元素(如类目标题里的小注释),只翻译第一个文本节点
    const first = el.firstChild;
    if (first && first.nodeType === Node.TEXT_NODE) first.textContent = t(el.dataset.i18n);
    else if (!first) el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  document.getElementById('lang-toggle').textContent = lang() === 'zh' ? 'EN' : '中文';
}

document.getElementById('lang-toggle').addEventListener('click', () => {
  setLang(lang() === 'zh' ? 'en' : 'zh');
  applyI18n();
  if (!$('catview').hidden) loadCategories();
  if (!$('mapview').hidden) loadMapView(true);
  if (!$('timelineview').hidden) loadTimeline();
  if (!$('threadsview').hidden) loadThreads();
});

async function boot() {
  applyI18n();
  try {
    cfg = await (await fetch('/api/config')).json();
  } catch (e) {
    setStatus('无法连接后端 API,请从 python -m xbm serve 打开本页');
    return;
  }
  if (!cfg.model_ready) {
    setStatus(`模型文件不可用(${cfg.model_name}),检查 XBM_MODEL_PATH`);
    return;
  }
  try {
    setStatus('加载嵌入运行时(wasm)…');
    await getOrLoadGlobalLiteRtLm('./wasm');
    setStatus('下载/加载模型中(WebGPU)…');
    const resp = await fetch('/model');
    const total = +resp.headers.get('Content-Length') || 0;
    const stream = trackProgress(resp.body, total, (n, t) =>
      setStatus(`加载模型 ${t ? Math.round((n / t) * 100) : Math.round(n / 1048576) + 'MB'}%`));
    const t0 = performance.now();
    // 纯文本 270m 模型不能带 vision/audio backend(会去找视觉编码器缓冲区)
    try {
      engine = await EmbeddingEngine.create({ model: stream, backend: Backend.GPU });
    } catch (e) {
      setStatus(`模型加载失败:${e}(需要支持 WebGPU 的 Chrome/Edge)`);
      return;
    }
    setStatus(`模型就绪(加载耗时 ${((performance.now() - t0) / 1000).toFixed(1)}s)`);
  } catch (e) {
    setStatus(`模型加载失败:${e}(需要支持 WebGPU 的 Chrome/Edge)`);
    return;
  }
  await refreshStatus();
  indexLoop();
}

async function refreshStatus() {
  try {
    const s = await (await fetch('/api/status')).json();
    $('embed-text').textContent = `嵌入进度 ${s.embedded}/${s.total}` + (s.total && s.embedded < s.total ? '(进行中…)' : '');
    $('embed-fill').style.width = s.total ? `${Math.round((s.embedded / s.total) * 100)}%` : '0%';
    if (!s.sync_configured) $('sync-note').textContent = '(尚未配置同步,先运行 python -m xbm sync --init)';
    if (s.twscrape) {
      const req = s.twscrape.requests;
      let line = `书签总量 ${s.total} · 同步已发 ${req ?? '?'} 次请求`;
      if (s.twscrape.locked_until) {
        const t = new Date(s.twscrape.locked_until.replace(' ', 'T') + 'Z');
        if (t.getTime() > Date.now()) {
          line += ` · 限速锁定中,${t.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 自动续跑`;
        } else {
          line += ' · 拉取中';
        }
      } else {
        line += ' · 拉取中/空闲';
      }
      $('backfill-note').textContent = line;
    }
    if (s.embed_job) {
      const j = s.embed_job;
      if (j.running) {
        $('embed-note').textContent =
          `批量嵌入(${j.backend})进行中: ${j.done}/${j.total}${j.rate ? ` · ${j.rate} 条/秒` : ''}`;
        setTimeout(refreshStatus, 3000);  // 任务运行时加速轮询
      } else if (j.error) {
        $('embed-note').textContent = `上次嵌入出错: ${j.error}`;
      } else if (j.last_result) {
        $('embed-note').textContent = j.last_result;
      }
    }
    return s;
  } catch (e) { return null; }
}

async function startBulkEmbed() {
  const backend = $('embed-backend').value;
  const btn = $('btn-embed');
  btn.disabled = true;
  try {
    const r = await (await fetch('/api/embed/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backend }),
    })).json();
    if (r.error) { $('embed-note').textContent = r.error; return; }
    $('embed-note').textContent = `已启动批量嵌入(${backend})…`;
    refreshStatus();
  } catch (e) {
    $('embed-note').textContent = `启动失败:${e}`;
  } finally {
    btn.disabled = false;
  }
}

async function indexLoop() {
  if (!engine || indexing) return;
  indexing = true;
  try {
    for (;;) {
      if (pauseIndexing) break;
      const rows = await (await fetch('/api/pending?limit=32')).json();
      if (!Array.isArray(rows) || rows.length === 0) break;
      const resps = await engine.computeEmbeddingBatch(
        rows.map((r) => r.text), { normalize: true });
      const vectors = rows.map((r, i) => ({ id: r.id, vec: Array.from(resps[i].embedding) }));
      await fetch('/api/embeddings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: cfg.model_name, vectors }),
      });
      await refreshStatus();
    }
  } catch (e) {
    $('embed-text').textContent = `嵌入中断:${e}`;
  } finally {
    indexing = false;
  }
}

async function embedQuery(q) {
  pauseIndexing = true;  // 排在下一批边界之前,避免被索引循环饿死
  try {
    const resp = await engine.computeEmbedding(cfg.query_prefix + q, { normalize: true });
    return Array.from(resp.embedding);
  } finally {
    pauseIndexing = false;
    indexLoop();
  }
}

async function doSearch() {
  const q = $('q').value.trim();
  if (!q) return;
  const mode = $('mode').value;
  let qvec = null;
  if (mode !== 'fts') {
    if (!engine) { renderNote('模型未就绪,无法做语义检索'); return; }
    try { qvec = await embedQuery(q); } catch (e) { renderNote(`查询向量失败:${e}`); return; }
  }
  const body = {
    q, qvec, mode, k: 50,
    filters: {
      author: $('f-author').value.trim() || null,
      after: $('f-after').value || null,
      before: $('f-before').value || null,
      has_media: $('f-media').checked,
    },
  };
  const res = await (await fetch('/api/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })).json();
  renderResults(res);
}

function renderResults(res) {
  const el = $('results');
  el.innerHTML = '';
  if (res.note) renderNote(res.note, el);
  if (!res.hits || !res.hits.length) {
    el.innerHTML += '<p class="muted">(无结果;试试混合模式、放宽过滤,或先完成同步与嵌入)</p>';
    return;
  }
  renderCards(res.hits, el);
}

function renderCards(hits, el, opts = {}) {
  const { withScores = true } = opts;
  for (const h of hits) {
    const div = document.createElement('div');
    div.className = 'hit';
    const badges = [];
    if (withScores && h.sim != null) badges.push(`<span class="badge">语义 ${(h.sim * 100).toFixed(0)}%</span>`);
    if (withScores && h.fts_hit) badges.push('<span class="badge fts">关键词✓</span>');
    const date = (h.date_sort || '').slice(0, 10);
    const srcUrl = `https://x.com/${h.author_handle || 'i'}/status/${h.tweet_id}`;
    const media = (h.media || []).map((m) =>
      `<img src="${m.url}" loading="lazy" onerror="this.remove()" alt="">`).join('');
    const urls = (h.urls || []).slice(0, 8).map((u) => {
      const t = u.length > 70 ? u.slice(0, 70) + '…' : u;
      return `<a href="${u}" target="_blank" rel="noreferrer">${t}</a>`;
    }).join('');
    const long = (h.text || '').length > 220;
    div.innerHTML = `
      <div class="meta">
        <span class="badges">${badges.join('')}</span>
        <b>@${h.author_handle || '?'}</b>
        <span>${date}</span>
        <span class="spacer"></span>
        <a class="src" href="${srcUrl}" target="_blank" rel="noreferrer">${t('card.src')}</a>
        <button class="del" data-id="${h.tweet_id}" title="local delete">${t('card.del')}</button>
      </div>
      <div class="text${long ? ' clamp' : ''}"></div>
      ${long ? `<button class="more">${t('card.more')}</button>` : ''}
      <div class="thumbs">${media}</div>
      <div class="links">${urls}</div>`;
    const textNode = div.querySelector('.text');
    textNode.textContent = h.text || '';
    const moreBtn = div.querySelector('.more');
    if (moreBtn) moreBtn.addEventListener('click', () => {
      const clamped = textNode.classList.toggle('clamp');
      moreBtn.textContent = clamped ? t('card.more') : t('card.less');
    });
    div.querySelector('.del').addEventListener('click', () => deleteBookmark(h.tweet_id, div));
    el.appendChild(div);
  }
}

function renderNote(msg, el) {
  const n = document.createElement('p');
  n.className = 'note';
  n.textContent = msg;
  (el || $('results')).prepend(n);
}

async function deleteBookmark(tweetId, cardEl) {
  if (!confirm(t('del.confirm'))) return;
  try {
    const r = await (await fetch('/api/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tweet_id: tweetId }),
    })).json();
    if (r.error) { alert(r.error); return; }
    cardEl.remove();
    refreshStatus();
  } catch (e) {
    alert(`删除失败:${e}`);
  }
}

let syncPollTimer = null;

async function pollSyncJob() {
  let s;
  try { s = await (await fetch('/api/status')).json(); } catch (e) { return; }
  const j = s.sync_job;
  if (!j) return;
  if (j.running) {
    $('sync-note').textContent =
      `同步中(${j.phase},后端 ${j.backend}):取回 ${j.fetched},新增 ${j.new}…`;
    syncPollTimer = setTimeout(pollSyncJob, 2000);
    return;
  }
  syncPollTimer = null;
  if (j.error) { $('sync-note').textContent = `同步失败:${j.error}`; return; }
  if (j.last_result) {
    $('sync-note').textContent = `同步完成:${j.last_result}`;
    await refreshStatus();
    indexLoop();
    if (!$('catview').hidden) loadCategories();
    if (!$('mapview').hidden) loadMapView(true);
  }
}

async function doSync() {
  const btn = $('sync');
  btn.disabled = true;
  try {
    const res = await (await fetch('/api/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })).json();
    if (res.error) { $('sync-note').textContent = res.error; return; }
    $('sync-note').textContent = '同步已启动(后台)…';
    clearTimeout(syncPollTimer);
    pollSyncJob();
  } catch (e) {
    $('sync-note').textContent = `同步出错:${e}`;
  } finally {
    btn.disabled = false;
  }
}

$('go').addEventListener('click', doSearch);
$('q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
$('sync').addEventListener('click', doSync);
$('btn-embed').addEventListener('click', startBulkEmbed);
setInterval(refreshStatus, 30000);  // 后台回填/嵌入进度持续可见
boot();


// ---------------- 分类浏览(三维度:类目 × 关键词聚类 × LLM 聚类) ----------------

const filterSel = { category: null, kw: null, llm: null };  // 当前交集筛选状态
const chipLabel = {};  // `${dim}:${key}` -> 显示名(交集标题用)

function switchView(name) {
  for (const v of ['search', 'cat', 'map', 'timeline', 'threads']) {
    $('view-' + v).classList.toggle('active', v === name);
  }
  $('searchview').hidden = name !== 'search';
  $('catview').hidden = name !== 'cat';
  $('mapview').hidden = name !== 'map';
  $('timelineview').hidden = name !== 'timeline';
  $('threadsview').hidden = name !== 'threads';
  if (name === 'cat') {
    loadCategories();
    resumeClusterJobIfNeeded();
  }
  if (name === 'map') loadMapView();
  if (name === 'timeline') loadTimeline();
  if (name === 'threads') loadThreads();
}

async function resumeClusterJobIfNeeded() {  // 页面刷新后接管进行中的聚类任务显示
  try {
    const s = await (await fetch('/api/status')).json();
    if (s.cluster_job && s.cluster_job.running && !clusterPollTimer) pollClusterJob();
  } catch (e) { /* 忽略 */ }
}

let catState = { defs: [] };

async function loadCategories() {
  try {
    const [cats, clusters] = await Promise.all([
      (await fetch('/api/categories')).json(),
      (await fetch('/api/clusters')).json(),
    ]);
    catState.defs = cats.defs || [];

    renderChipRow('cat-chips', (cats.counts || []).map(c => ({
      key: c.name, label: `${c.name}`, n: c.count, dim: 'category',
    })), t('cat.empty'));

    const sets = clusters.clusters || {};
    renderChipRow('kw-chips', (sets.keyword || []).slice(0, 60).map(c => ({
      key: c.cluster_id, label: c.name, n: c.size, dim: 'kw',
    })), t('kw.empty'),
      (sets.keyword || []).length);
    renderChipRow('llm-chips', (sets.llm || []).slice(0, 60).map(c => ({
      key: c.cluster_id, label: c.name, n: c.size, dim: 'llm',
    })), t('llm.empty'),
      (sets.llm || []).length);

    if (clusters.options) {  // 预填已保存的 LLM 配置
      if (clusters.options.llm_base && !$('llm-base').value)
        $('llm-base').value = clusters.options.llm_base;
      if (clusters.options.llm_model && !$('llm-model').value)
        $('llm-model').value = clusters.options.llm_model;
      if (clusters.options.llm_style) $('llm-style').value = clusters.options.llm_style;
    }
  } catch (e) {
    $('cat-note').textContent = `加载分类失败:${e}`;
  }
}

function renderChipRow(boxId, items, emptyText, totalCount) {
  const box = $(boxId);
  box.innerHTML = '';
  if (!items.length) {
    box.innerHTML = `<span class="muted">${emptyText}</span>`;
    return;
  }
  for (const it of items) {
    chipLabel[`${it.dim}:${it.key}`] = it.label;
    const b = document.createElement('button');
    b.className = 'chip' + (filterSel[it.dim] === it.key ? ' on' : '');
    b.innerHTML = `${it.label} <span class="n">${it.n}</span>`;
    b.addEventListener('click', () => {
      filterSel[it.dim] = filterSel[it.dim] === it.key ? null : it.key;  // 再点取消
      loadCategories();
      refreshBrowse();
    });
    box.appendChild(b);
  }
  if (totalCount && totalCount > items.length) {
    box.insertAdjacentHTML('beforeend',
      `<span class="muted">…共 ${totalCount} 簇,按规模显示前 ${items.length}</span>`);
  }
}

let browseOffset = 0;

async function refreshBrowse() {
  browseOffset = 0;
  $('cat-results').innerHTML = '';
  const active = Object.entries(filterSel).filter(([, v]) => v != null);
  if (!active.length) {
    $('cat-header').innerHTML = '';
    return;
  }
  await loadMoreBrowse();
}

async function loadMoreBrowse() {
  const params = new URLSearchParams({ offset: browseOffset, limit: 50 });
  if (filterSel.category != null) params.set('category', filterSel.category);
  if (filterSel.kw != null) params.set('kw', filterSel.kw);
  if (filterSel.llm != null) params.set('llm', filterSel.llm);
  const r = await (await fetch(`/api/browse?${params}`)).json();
  if (r.error) { $('cat-note').textContent = r.error; return; }
  if (browseOffset === 0) {
    const parts = [];
    if (filterSel.category != null) parts.push(`类目「${filterSel.category}」`);
    if (filterSel.kw != null) parts.push(`关键词簇「${chipLabel['kw:' + filterSel.kw] ?? '#' + filterSel.kw}」`);
    if (filterSel.llm != null) parts.push(`LLM簇「${chipLabel['llm:' + filterSel.llm] ?? '#' + filterSel.llm}」`);
    $('cat-header').innerHTML =
      `<h3 style="margin-bottom:8px">${parts.join(' ∩ ')} — 共 ${r.total} 条</h3>`;
  }
  renderCards(r.hits, $('cat-results'), { withScores: false });
  browseOffset += r.hits.length;
  if (r.has_more) {
    const more = document.createElement('button');
    more.className = 'ghost more-page';
    more.textContent = '加载更多';
    more.addEventListener('click', () => { more.remove(); loadMoreBrowse(); });
    $('cat-results').appendChild(more);
  }
}

async function doClassify() {
  const btn = $('btn-classify');
  btn.disabled = true;
  $('cat-note').textContent = '正在生成分类(服务端嵌入类目描述 + 全量分配)…';
  try {
    let r = await (await fetch('/api/categories/embed', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ defs: catState.defs, backend: $('embed-backend').value }),
    })).json();
    if (r.error && engine) {  // 服务端没有 litert_lm 时,浏览器 WebGPU 兜底
      const resps = await engine.computeEmbeddingBatch(
        catState.defs.map((d) => cfg.doc_prefix + d.desc), { normalize: true });
      r = await (await fetch('/api/categories/embed', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          categories: catState.defs.map((d, i) => ({
            name: d.name, vec: Array.from(resps[i].embedding) })),
        }),
      })).json();
    }
    if (r.error) throw new Error(r.error);
    $('cat-note').textContent = `已分配 ${r.assigned} 条`;
    loadCategories();
  } catch (e) {
    $('cat-note').textContent = `分类失败:${e}`;
  } finally {
    btn.disabled = false;
  }
}

async function testLlm() {
  const btn = $('btn-llm-test');
  const out = $('llm-test-result');
  btn.disabled = true;
  out.textContent = '测试中…';
  try {
    const r = await (await fetch('/api/llm/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        llm_base: $('llm-base').value.trim(),
        llm_model: $('llm-model').value.trim(),
        llm_style: $('llm-style').value,
      }),
    })).json();
    out.textContent = (r.ok ? '✓ ' : '✗ ') + r.detail +
      (r.latency_ms != null ? `(${r.latency_ms}ms)` : '');
    out.style.color = r.ok ? 'var(--good)' : '#e5735f';
  } catch (e) {
    out.textContent = `✗ 测试请求失败:${e}`;
    out.style.color = '#e5735f';
  } finally {
    btn.disabled = false;
  }
}

let clusterPollTimer = null;

async function pollClusterJob() {
  let s;
  try {
    s = await (await fetch('/api/status')).json();
  } catch (e) { return; }
  const j = s.cluster_job;
  if (!j) return;
  if (j.running) {
    if (j.phase === 'naming') {
      $('cat-note').textContent =
        `命名中:第 ${j.done}/${j.total} 簇` + (j.current ? `,当前:${j.current}` : '');
    } else {
      $('cat-note').textContent =
        `聚类中(${j.granularity ?? ''}):${j.current || '准备中'}…`;
    }
    clusterPollTimer = setTimeout(pollClusterJob, 2000);
    return;
  }
  clusterPollTimer = null;
  if (j.error) {
    $('cat-note').textContent = `聚类失败:${j.error}`;
    return;
  }
  if (j.result) {
    $('cat-note').textContent = `完成:${j.result.note ?? ''}`;
    loadCategories();
  }
}

async function doCluster() {
  const btn = $('btn-cluster');
  btn.disabled = true;
  const set = $('cluster-set').value;
  const useLlm = set === 'llm';
  $('llm-fields').hidden = !useLlm;
  try {
    const body = {
      set,
      granularity: $('cluster-granularity').value,
      recluster: $('cluster-recluster').checked,
    };
    if (useLlm) {
      body.llm_base = $('llm-base').value.trim();
      body.llm_model = $('llm-model').value.trim();
      body.llm_style = $('llm-style').value;
      if (!body.llm_base) throw new Error('请填写 LLM API 地址(如 llama-server 的 http://127.0.0.1:8080/v1)');
    }
    const r = await (await fetch('/api/clusters/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })).json();
    if (r.error) throw new Error(r.error);
    $('cat-note').textContent = `任务已启动(${useLlm ? 'LLM' : '关键词'}分区${body.recluster ? ',重新聚类' : ',仅补命名'})…`;
    clearTimeout(clusterPollTimer);
    pollClusterJob();
  } catch (e) {
    $('cat-note').textContent = `失败:${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
}

$('view-search').addEventListener('click', () => switchView('search'));
$('view-cat').addEventListener('click', () => switchView('cat'));
$('btn-classify').addEventListener('click', doClassify);
$('btn-cluster').addEventListener('click', doCluster);
$('btn-llm-test').addEventListener('click', testLlm);
$('cluster-set').addEventListener('change', () => {
  $('llm-fields').hidden = $('cluster-set').value !== 'llm';
});

// ---------------- 地图(UMAP/PCA 2D 散点) ----------------

const PALETTE = ['#4f8cff','#3fb96f','#e5a13d','#e5735f','#b06fd8','#4fc3d9',
  '#c9db4a','#ff8fab','#8d7bff','#5fd3a2','#f2a54f','#7f9cf5','#d84f7f','#63c984',
  '#a0d8ef','#ffd166','#9be564','#f4845f','#c4bbf0','#78c6a3'];
let mapData = null;
let mapT = { scale: 1, tx: 0, ty: 0 };

function mapGeom() {
  const c = $('map-canvas');
  return { c, ctx: c.getContext('2d'), W: c.clientWidth, H: c.clientHeight };
}

function mapXY(p, W, H) {
  const s = mapT.scale;
  return [ (p.x * W + mapT.tx) * s + W * (1 - s) / 2,
           (p.y * H + mapT.ty) * s + H * (1 - s) / 2 ];
}

function drawMap() {
  const { c, ctx, W, H } = mapGeom();
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(W * dpr)) {
    c.width = Math.round(W * dpr);
    c.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!mapData || !mapData.points.length) return;
  const gidx = Object.keys(mapData.groups || {});
  for (const p of mapData.points) {
    const gi = p.g == null ? -1 : gidx.indexOf(String(p.g));
    ctx.fillStyle = p.g == null ? 'rgba(139,147,163,.30)'
                                : PALETTE[Math.max(gi, 0) % PALETTE.length];
    const [x, y] = mapXY(p, W, H);
    ctx.fillRect(x - 1.3, y - 1.3, 2.8, 2.8);
  }
}

function renderLegend(d) {
  const box = $('map-legend');
  box.innerHTML = '';
  Object.entries(d.groups || {}).slice(0, 26).forEach(([g, label], i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `<i style="width:10px;height:10px;border-radius:5px;display:inline-block;background:${PALETTE[i % PALETTE.length]}"></i> ${label}`;
    box.appendChild(chip);
  });
}

async function loadMapView(force = false) {
  const note = $('map-note');
  try {
    const s = await (await fetch('/api/status')).json();
    const j = s.map_job;
    if (j && j.running) {
      note.textContent = `地图生成中:${j.phase || '计算中'}(首次 UMAP 约 1-2 分钟)…`;
      setTimeout(() => loadMapView(true), 3000);
      return;
    }
    if (j && j.error) note.textContent = `上次地图出错:${j.error}`;
    if (!mapData || force) {
      const r = await (await fetch(`/api/map/points?color=${$('map-color').value}`)).json();
      if (!r.points.length) {
        note.textContent = '尚无地图坐标——点「重新生成地图」';
        return;
      }
      mapData = r;
      mapT = { scale: 1, tx: 0, ty: 0 };
      note.textContent = `${r.points.length} · ${r.job && r.job.last_result || ''} · ${t('map.zoomHint')}`;
    }
    renderLegend(mapData);
    drawMap();
  } catch (e) {
    note.textContent = `地图加载失败:${e}`;
  }
}

async function genMap() {
  const btn = $('btn-map-gen');
  btn.disabled = true;
  try {
    const r = await (await fetch('/api/map/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method: 'umap' }),
    })).json();
    if (r.error) { $('map-note').textContent = r.error; return; }
    mapData = null;
    loadMapView(true);
  } finally {
    btn.disabled = false;
  }
}

function nearestPoint(mx, my) {
  if (!mapData) return null;
  const { W, H } = mapGeom();
  let best = null, bd = 100;  // 屏幕像素平方距离阈值
  for (const p of mapData.points) {
    const [x, y] = mapXY(p, W, H);
    const d = (x - mx) ** 2 + (y - my) ** 2;
    if (d < bd) { bd = d; best = p; }
  }
  return best;
}

function groupLabel(g) {
  if (g == null || !mapData) return '未分组';
  return mapData.groups[String(g)] || '#' + g;
}

(() => {
  const c = $('map-canvas');
  let drag = null;
  c.addEventListener('mousedown', (e) => {
    drag = { x: e.offsetX, y: e.offsetY, tx: mapT.tx, ty: mapT.ty, moved: false };
  });
  c.addEventListener('mousemove', (e) => {
    if (drag) {
      const dx = e.offsetX - drag.x, dy = e.offsetY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      mapT.tx = drag.tx + dx;
      mapT.ty = drag.ty + dy;
      $('map-tip').hidden = true;
      drawMap();
      return;
    }
    const p = nearestPoint(e.offsetX, e.offsetY);
    const tip = $('map-tip');
    if (p) {
      tip.hidden = false;
      tip.style.left = (e.offsetX + 14) + 'px';
      tip.style.top = (e.offsetY + 10) + 'px';
      tip.textContent = `[${groupLabel(p.g)}] ${p.t}`;
    } else {
      tip.hidden = true;
    }
  });
  c.addEventListener('mouseleave', () => { $('map-tip').hidden = true; drag = null; });
  c.addEventListener('mouseup', (e) => {
    const wasDrag = drag && drag.moved;
    drag = null;
    if (wasDrag) return;
    const p = nearestPoint(e.offsetX, e.offsetY);
    if (!p) { $('map-card').hidden = true; return; }
    const card = $('map-card');
    card.hidden = false;
    card.innerHTML = `<span class="close">✕ 关闭</span>
      <div class="muted">${groupLabel(p.g)}</div>
      <div class="text"></div>
      <a href="https://x.com/i/status/${p.id}" target="_blank" rel="noreferrer">${t('card.src')}</a>`;
    card.querySelector('.text').textContent = p.t + '…';
    card.querySelector('.close').onclick = () => { card.hidden = true; };
  });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    mapT.scale = Math.min(30, Math.max(0.5, mapT.scale * f));
    drawMap();
  }, { passive: false });
  $('map-color').addEventListener('change', () => { mapData = null; loadMapView(true); });
  $('btn-map-gen').addEventListener('click', genMap);
  $('view-map').addEventListener('click', () => switchView('map'));
  window.addEventListener('resize', () => { if (!$('mapview').hidden) drawMap(); });
})();

// ---------------- 时间脉络(关注焦点演变) ----------------

let timelinePollTimer = null;

async function loadTimeline() {
  try {
    const r = await (await fetch(`/api/timeline?unit=${$('timeline-unit').value}&window=${$('timeline-window').value}`)).json();
    const note = $('timeline-note');
    if (r.job && r.job.running) {
      note.textContent = `生成中(${r.job.phase === 'narrating' ? 'LLM 撰写叙事,可能需要一两分钟' : '统计聚合'})…`;
      clearTimeout(timelinePollTimer);
      timelinePollTimer = setTimeout(async () => { await loadTimeline(); renderTimeline(r.timeline); }, 3000);
    } else if (r.job && r.job.error) {
      note.textContent = `出错:${r.job.error}`;
    } else if (r.job && r.job.last_result) {
      note.textContent = r.job.last_result;
    } else if (!r.timeline) {
      note.textContent = '尚未生成——点「生成 / 重新生成」';
    }
    renderTimeline(r.timeline);
  } catch (e) {
    $('timeline-note').textContent = `加载失败:${e}`;
  }
}

function renderTimeline(t) {
  const narr = $('timeline-narrative');
  const box = $('timeline-digest');
  narr.textContent = '';
  box.innerHTML = '';
  if (!t) return;
  if (t.narrative) {
    narr.textContent = t.narrative;
  } else {
    narr.innerHTML = '<span class="muted">(暂无 LLM 叙事——统计见下方原始数据)</span>';
  }
  for (const d of t.digest || []) {
    const div = document.createElement('div');
    div.className = 'tl-period';
    const cats = (d.cats || []).map(([k, v]) => `${k} ${v}`).join(', ');
    const llm = (d.llm || []).map(([k, v]) => `${k} ${v}`).join(', ');
    const kw = (d.kw || []).map(([k, v]) => `${k} ${v}`).join(', ');
    div.innerHTML = `<b>${d.period}</b>${d.total} 条`
      + `<div class="muted">类目:${cats || '-'}<br>主题:${llm || '-'}<br>关键词:${kw || '-'}</div>`
      + (d.samples || []).map(s => `<div class="muted">例:「${s}」</div>`).join('');
    box.appendChild(div);
  }
}

async function runTimeline() {
  const btn = $('btn-timeline');
  btn.disabled = true;
  try {
    const r = await (await fetch('/api/timeline/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit: $('timeline-unit').value, window: $('timeline-window').value }),
    })).json();
    if (r.error) { $('timeline-note').textContent = r.error; return; }
    $('timeline-note').textContent = '已启动…';
    loadTimeline();
  } catch (e) {
    $('timeline-note').textContent = `启动失败:${e}`;
  } finally {
    btn.disabled = false;
  }
}

$('btn-timeline').addEventListener('click', runTimeline);
$('timeline-unit').addEventListener('change', loadTimeline);
$('timeline-window').addEventListener('change', loadTimeline);
$('view-timeline').addEventListener('click', () => { switchView('timeline'); });

// ---------------- 线索(兴趣账本:中断/持续/新芽/脉冲) ----------------

const SEC_META = {
  dormant: ['sec.dormant', 'sec.dormant.sub', true],
  persistent: ['sec.persistent', 'sec.persistent.sub', false],
  emerging: ['sec.emerging', 'sec.emerging.sub', false],
  pulse: ['sec.pulse', 'sec.pulse.sub', false],
};
let threadsPollTimer = null;

async function loadThreads() {
  try {
    const r = await (await fetch('/api/threads')).json();
    renderThreads(r);
    const j = r.cards_job;
    if (j && j.running) {
      $('threads-note').textContent =
        `解读生成中:${j.done}/${j.total}…`;
      clearTimeout(threadsPollTimer);
      threadsPollTimer = setTimeout(loadThreads, 5000);
    } else if (j && j.error) {
      $('threads-note').textContent = `解读失败:${j.error}`;
    } else if (j && j.last_result) {
      $('threads-note').textContent = j.last_result;
    }
  } catch (e) {
    $('threads-note').textContent = `加载失败:${e}`;
  }
}

function showThreadBookmarks(set, id, name) {
  switchView('cat');
  filterSel.category = null;
  filterSel.kw = set === 'keyword' ? id : null;
  filterSel.llm = set === 'llm' ? id : null;
  loadCategories();
  refreshBrowse();
}

function renderThreads(r) {
  const box = $('threads-body');
  box.innerHTML = '';
  const cards = r.cards || {};
  for (const cls of ['dormant', 'persistent', 'emerging', 'pulse']) {
    const list = (r.threads || {})[cls] || [];
    const [titleKey, subKey, warn] = SEC_META[cls];
    const title = t(titleKey), sub = t(subKey);
    const sec = document.createElement('div');
    sec.innerHTML = `<div class="thread-sec"><b>${title}</b>(${list.length}) <span class="muted">${sub}</span></div>`;
    box.appendChild(sec);
    for (const t of list.slice(0, cls === 'pulse' ? 15 : 40)) {
      const div = document.createElement('div');
      div.className = 'thread-card' + (warn && cls === 'dormant' ? ' dormant' : '');
      const card = cards[`${t.set}:${t.cluster_id}`];
      const stats = [
        `${t.size} 条`, `活跃 ${t.active_months} 个月`,
        `${t.first} ~ ${t.last}`,
        cls === 'dormant' ? `<span class="warn">已沉寂 ${t.dormancy} 个月</span>` : '',
        t.recent3 ? `近3月 ${t.recent3} 条` : '',
      ].filter(Boolean).join(' · ');
      div.innerHTML = `
        <div class="head">
          <b data-set="${t.set}" data-id="${t.cluster_id}">${t.name}</b>
          <span class="spark">${t.spark}</span>
        </div>
        <div class="stats">${stats}</div>
        ${card ? `<div class="cardllm">
            <div><span class="lbl">是什么</span>${card.about || ''}</div>
            <div><span class="lbl">下一步</span>${card.next || ''}</div>
          </div>` : ''}`;
      div.querySelector('b').addEventListener('click', () =>
        showThreadBookmarks(t.set, t.cluster_id, t.name));
      box.appendChild(div);
    }
  }
}

async function runThreadCards() {
  const btn = $('btn-thread-cards');
  btn.disabled = true;
  try {
    const r = await (await fetch('/api/threads/cards/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })).json();
    if (r.error) { $('threads-note').textContent = r.error; return; }
    $('threads-note').textContent = '解读已启动…';
    loadThreads();
  } catch (e) {
    $('threads-note').textContent = `启动失败:${e}`;
  } finally {
    btn.disabled = false;
  }
}
$('btn-thread-cards').addEventListener('click', runThreadCards);
$('view-threads').addEventListener('click', () => switchView('threads'));
