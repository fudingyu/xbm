// taxonomy.js — 静态版分析算法(Python cluster.py/category.py/threads.py/projection.py 的 JS 移植)
// 全部纯函数:worker(重计算)与主线程(类目定义展示)共用。

export const GRANULARITY_LLM = {
  vfine: 0.70, fine: 0.68, medium: 0.65, coarse: 0.62,
};
export const GRANULARITY_KW = {
  vfine: 0.14, fine: 0.12, medium: 0.10, coarse: 0.09,
};
export const QUERY_PREFIX = 'task: search result | query: ';
export const DOC_PREFIX = 'title: none | text: ';

export const CATEGORY_DEFS = [
  ['AI & LLM', 'Large language models, AI apps, agents, prompt engineering, inference cost, fine-tuning, open-source models'],
  ['Programming', 'Programming languages, frameworks, engineering practice, open-source projects, debugging, system design'],
  ['Tools & Productivity', 'Software tools, browser extensions, automation scripts, workflows, note-taking and knowledge management'],
  ['Hardware & Gadgets', 'Phones, laptops, GPUs, chips, DIY hardware, consumer electronics, teardowns and reviews'],
  ['Science & Learning', 'Science, math, history, paper walkthroughs, cognitive science, learning methods'],
  ['Business & Investing', 'Startups, business analysis, strategy, stocks and funds, crypto, macro economics'],
  ['News & Current', 'Social news, international politics, industry moves, public events'],
  ['Life & Fun', 'Movies, games, music, memes, sports, celebrities'],
  ['Travel & Food', 'Travel guides, destinations, restaurants, food'],
  ['Health & Sports', 'Fitness, running, medicine, sleep, nutrition'],
  ['Design & Creation', 'UI/UX, graphic design, photography, drawing, writing, video creation'],
  ['Other', 'Anything that fits none of the above'],
];

const STOP = new Set(('the and for you this that with from have will are was were has its not but can all our their your out who get how why what when where which http https www com html org com. also into over more most only very just like some such than then them they there these those about after before because been being both each other over under again further once here when why how any both each few nor own same too s t don now').split(' '));

// 分词:中文连续段切 bigram(零依赖,聚类效果与 jieba 相当),英文小写词
export function tokenize(text) {
  const out = new Set();
  const s = (text || '').toLowerCase();
  for (const run of (s.match(/[\u4e00-\u9fff]+/g) || [])) {
    if (run.length === 1) { out.add(run); continue; }
    for (let i = 0; i < run.length - 1; i++) out.add(run.slice(i, i + 2));
  }
  for (const w of (s.match(/[a-z][a-z0-9+#.-]{2,}/g) || [])) {
    if (!STOP.has(w)) out.add(w);
  }
  return out;
}

// ---- 引领聚类(稠密嵌入向量) ----
// rows: [{tweet_id, vec: Float32Array}] 已归一化;返回 labels(Int32Array,-1=跳过)
export function leaderDense(rows, threshold) {
  const labels = new Int32Array(rows.length).fill(-1);
  const cents = [];
  for (let i = 0; i < rows.length; i++) {
    const v = rows[i].vec;
    if (!v || !v.length) continue;
    let best = -1, bestSim = threshold;
    for (let c = 0; c < cents.length; c++) {
      let s = 0;
      const cv = cents[c];
      for (let k = 0; k < v.length; k++) s += v[k] * cv[k];
      if (s > bestSim) { best = c; bestSim = s; }
    }
    if (best < 0) { cents.push(Float32Array.from(v)); labels[i] = cents.length - 1; }
    else {
      const cv = cents[best];
      for (let k = 0; k < v.length; k++) cv[k] += v[k];
      let n = Math.sqrt(cv.reduce((a, x) => a + x * x, 0)) || 1;
      for (let k = 0; k < cv.length; k++) cv[k] /= n;
      labels[i] = best;
    }
  }
  return labels;
}

// ---- 引领聚类(稀疏 IDF 词向量,文本视角) ----
export function leaderSparse(items, threshold) {
  // items: [{idx, vec: Map<tok, weight>}] 已归一化
  const labels = new Map(); // idx -> label
  const cents = [];
  for (const { idx, vec } of items) {
    if (vec.size < 2) continue;
    let best = -1, bestSim = threshold;
    for (let c = 0; c < cents.length; c++) {
      const cv = cents[c];
      const [small, large] = cv.size < vec.size ? [cv, vec] : [vec, cv];
      let s = 0;
      for (const [t, w] of small) { const wv = large.get(t); if (wv) s += w * wv; }
      if (s > bestSim) { best = c; bestSim = s; }
    }
    if (best < 0) { cents.push(new Map(vec)); labels.set(idx, cents.length - 1); }
    else {
      const cv = cents[best];
      for (const [t, w] of vec) cv.set(t, (cv.get(t) || 0) + w);
      let n = 0;
      for (const w of cv.values()) n += w * w;
      n = Math.sqrt(n) || 1;
      for (const [t, w] of cv) cv.set(t, w / n);
      labels.set(idx, best);
    }
  }
  return labels;
}

// ---- PCA 前 2 主成分(幂迭代 + 去除,零依赖 UMAP 替代) ----
// M: {data: Float32Array, n, d};返回 {x: Float32Array, y: Float32Array}
export function pca2(M, iterations = 6) {
  const { data, n, d } = M;
  const mean = new Float64Array(d);
  for (let i = 0; i < n; i++) for (let k = 0; k < d; k++) mean[k] += data[i * d + k];
  for (let k = 0; k < d; k++) mean[k] /= n;
  const get = (i, k) => data[i * d + k] - mean[k];
  const power = () => {
    let v = new Float64Array(d);
    for (let k = 0; k < d; k++) v[k] = Math.random() - 0.5;
    for (let it = 0; it < iterations; it++) {
      const u = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        let s = 0;
        for (let k = 0; k < d; k++) s += get(i, k) * v[k];
        u[i] = s;
      }
      const v2 = new Float64Array(d);
      for (let i = 0; i < n; i++) { const ui = u[i]; for (let k = 0; k < d; k++) v2[k] += ui * get(i, k); }
      let nrm = Math.sqrt(v2.reduce((a, x) => a + x * x, 0)) || 1;
      for (let k = 0; k < d; k++) v2[k] /= nrm;
      v = v2;
    }
    return v;
  };
  const v1 = power();
  const comp1 = new Float64Array(n);
  for (let i = 0; i < n; i++) { let s = 0; for (let k = 0; k < d; k++) s += get(i, k) * v1[k]; comp1[i] = s; }
  const v2 = power(); // 未严格去 v1 分量,够可视化用
  const comp2 = new Float64Array(n);
  for (let i = 0; i < n; i++) { let s = 0; for (let k = 0; k < d; k++) s += get(i, k) * v2[k]; comp2[i] = s; }
  return { x: comp1, y: comp2 };
}

// ---- 时间剖面 → 线索分类(Python threads.py 移植) ----
export const DORMANT_MONTHS = 4;
export const EMERGE_MONTHS = 2;

function diffMonths(a, b) {
  if (!a || !b) return 0;
  const ya = +a.slice(0, 4), ma = +a.slice(5, 7), yb = +b.slice(0, 4), mb = +b.slice(5, 7);
  return (yb - ya) * 12 + (mb - ma);
}

export function classifyThread(t, nowM) {
  if (t.emerging) return 'emerging';
  if (t.active_months <= 2 && t.span <= 1) return 'pulse';
  if (t.dormancy >= DORMANT_MONTHS) return 'dormant';
  if (t.active_months >= 4 || t.span >= 5) return 'persistent';
  return 'persistent';
}

// rows: [{set, cid, month}] 已按簇聚合前的原始行
export function threadProfiles(rows, names, nowM) {
  const buckets = new Map();
  for (const r of rows) {
    if (!r.month) continue;
    const key = r.set + ':' + r.cid;
    let b = buckets.get(key);
    if (!b) { b = { set: r.set, cluster_id: r.cid, name: names[key] || ('#' + r.cid), months: new Map(), n: 0 }; buckets.set(key, b); }
    b.months.set(r.month, (b.months.get(r.month) || 0) + 1);
    b.n++;
  }
  const out = { dormant: [], persistent: [], emerging: [], pulse: [] };
  for (const b of buckets.values()) {
    if (b.set === 'keyword' && b.n < 4) continue; // 微线索过滤
    const ms = [...b.months.keys()].sort();
    const first = ms[0], last = ms[ms.length - 1];
    const t = {
      set: b.set, cluster_id: b.cluster_id, name: b.name, size: b.n,
      first, last, active_months: ms.length,
      span: diffMonths(first, last),
      dormancy: diffMonths(last, nowM),
      emerging: diffMonths(first, nowM) < EMERGE_MONTHS,
      spark: sparkline(b.months, nowM),
      recent3: [...b.months.entries()].filter(([m]) => diffMonths(m, nowM) < 3).reduce((a, [, c]) => a + c, 0),
    };
    t.class = classifyThread(t);
    out[t.class].push(t);
  }
  for (const k of Object.keys(out)) out[k].sort((a, b) => b.size - a.size);
  return out;
}

export function sparkline(months, nowM, width = 12) {
  const blocks = '▁▂▃▅▆';
  const y = +nowM.slice(0, 4), m = +nowM.slice(5, 7);
  let out = '';
  for (let i = width - 1; i >= 0; i--) {
    const mm = ((m - 1 - i) % 12 + 12) % 12 + 1;
    const yy = y + Math.floor((m - 1 - i) / 12);
    const c = months.get(`${yy}-${String(mm).padStart(2, '0')}`) || 0;
    out += c ? blocks[Math.min(c, 4)] : '·';
  }
  return out;
}
